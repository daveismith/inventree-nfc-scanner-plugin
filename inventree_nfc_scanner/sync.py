"""The exchange with a network scanner: one `/sync` call in, one answer out.

The contract is in docs/api.md. The rules that matter:

- Every command the server sends is numbered and sent again on every call until the
  scanner's `ack` covers it. Every message the scanner sends is numbered too; one the server
  has already seen (same reader, boot, seq) is acknowledged and not applied again.
- A `program` job's id on the wire is the Job's primary key, so each event finds its row.
- A `tag` event outside a job is the scanner's last tap, kept in the machine's state.
- With long polling on, a call with nothing to deliver is held, polling the queue, until a
  command appears or the scanner's `wait_s` runs out.
"""

from __future__ import annotations

import datetime
import logging
import time

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .machine import NfcScannerMachine, NfcScannerStatus
from .models import Job, ScannerCommand, ScannerMessage

logger = logging.getLogger('inventree')

PROTO_VERSION = 1
POLL_STEP_S = 0.25

# The scanner's error codes that mean "the tag is not blank and I was not told to overwrite".
NOT_BLANK = 'not_blank'


def enqueue(machine_config, payload: dict, job: Job | None = None) -> ScannerCommand:
    """Queue a command for a scanner, with the next sequence number."""
    with transaction.atomic():
        last = ScannerCommand.objects.filter(machine=machine_config).aggregate(m=Max('seq'))['m'] or 0
        return ScannerCommand.objects.create(machine=machine_config, seq=last + 1, job=job, payload=payload)


def pending_commands(machine_config):
    """Commands not yet acknowledged, oldest first."""
    return ScannerCommand.objects.filter(machine=machine_config, acked_at__isnull=True).order_by('seq')


def _finish(job: Job, state: str, **fields) -> None:
    for key, value in fields.items():
        setattr(job, key, value)
    job.state = state
    job.finished_at = timezone.now()
    job.save()


def _link_barcode(job: Job) -> None:
    """Make the tag's UID the location's barcode, taking it from whatever held it before."""
    from .barcodes import link_uid

    if not job.uid:
        return
    try:
        outcome = link_uid(job.location, job.uid)
        if outcome.startswith('moved'):
            job.error_detail = f'barcode {outcome}'[:200]
            job.save()
    except Exception as exc:  # noqa: BLE001 - the job succeeded; the link is reported, not fatal
        logger.warning('NFC job %s: could not link UID %s to %s: %s', job.pk, job.uid, job.location, exc)
        job.error_detail = f'tag written; barcode link failed: {exc}'[:200]
        job.save()


def apply_message(machine: NfcScannerMachine, msg: dict) -> None:
    """Act on one message from the scanner."""
    job_id = msg.get('id')
    job = None
    if isinstance(job_id, int):
        job = Job.objects.filter(pk=job_id, machine=machine.machine_config).first()

    if 'rsp' in msg:
        # The answer to a command. Only a refused job matters here.
        if job and not msg.get('ok', False) and not job.finished:
            _finish(job, Job.State.FAILED, error=msg.get('error', 'refused'), error_detail=msg.get('detail', ''))
        return

    evt = msg.get('evt')
    if evt == 'tag':
        machine.set_last_tag({
            'uid': msg.get('uid'),
            'type': msg.get('type'),
            'text': msg.get('text'),
            'uri': msg.get('uri'),
            'protected': msg.get('protected'),
            'error': msg.get('error'),
            'at': timezone.now().isoformat(),
        })
        return

    if job is None or job.finished:
        return

    if evt == 'waiting':
        job.state = Job.State.WAITING
        job.save()
        machine.set_status(NfcScannerStatus.BUSY)
    elif evt == 'writing':
        job.state = Job.State.WRITING
        job.uid = msg.get('uid', '') or ''
        job.save()
    elif evt == 'done':
        _finish(
            job, Job.State.DONE,
            uid=msg.get('uid', '') or job.uid, tag_type=msg.get('type', '') or '',
            protected=msg.get('protected'),
        )
        if job.kind == Job.Kind.PROGRAM:
            _link_barcode(job)
        machine.set_status(NfcScannerStatus.ONLINE)
    elif evt == 'failed':
        error = msg.get('error', 'failed')
        state = Job.State.CANCELLED if error == 'cancelled' else Job.State.FAILED
        _finish(
            job, state,
            error=error, error_detail=msg.get('detail', '') or '',
            uid=msg.get('uid', '') or job.uid,
            existing_text=(msg.get('text') or '')[:128] if error == NOT_BLANK else '',
            existing_uri=(msg.get('uri') or '')[:256] if error == NOT_BLANK else '',
        )
        machine.set_status(NfcScannerStatus.ONLINE)


def handle_sync(machine: NfcScannerMachine, body: dict, *, long_poll_max_s: int) -> dict:
    """Process one /sync call and build its answer."""
    config = machine.machine_config
    boot = int(body.get('boot', 0))
    ack = int(body.get('ack', 0))
    wait_s = max(0, min(int(body.get('wait_s', 0) or 0), long_poll_max_s))
    msgs = body.get('msgs') or []

    machine.touch(boot)
    if machine.status in (NfcScannerStatus.OFFLINE, NfcScannerStatus.UNKNOWN):
        machine.set_status(NfcScannerStatus.ONLINE)
    machine.set_status_text(str(timezone.now().strftime('last seen %Y-%m-%d %H:%M:%S UTC')))

    # 1. What the scanner has acted on is done with.
    now = timezone.now()
    ScannerCommand.objects.filter(machine=config, acked_at__isnull=True, seq__lte=ack).update(acked_at=now)

    # 2. Apply what it reports, once.
    highest = 0
    for msg in msgs:
        seq = msg.get('seq')
        if not isinstance(seq, int):
            continue
        highest = max(highest, seq)
        _, fresh = ScannerMessage.objects.get_or_create(machine=config, boot=boot, seq=seq)
        if fresh:
            try:
                apply_message(machine, msg)
            except Exception:  # one bad message must not stall the exchange
                logger.exception('NFC scanner %s: message %s not applied', config.pk, msg)

    # 3. Hand out what is waiting; hold the call for more if asked and allowed.
    deadline = time.monotonic() + wait_s
    while True:
        commands = list(pending_commands(config))
        if commands or time.monotonic() >= deadline:
            break
        time.sleep(POLL_STEP_S)

    sent_at = timezone.now()
    for command in commands:
        if command.sent_at is None:
            command.sent_at = sent_at
            command.save(update_fields=['sent_at'])
            if command.job and command.job.state == Job.State.QUEUED:
                command.job.state = Job.State.SENT
                command.job.save(update_fields=['state', 'updated_at'])

    return {
        'ack': highest,
        'cmds': [dict(command.payload, seq=command.seq) for command in commands],
    }


def mark_stale_scanners(offline_after_s: int) -> None:
    """Scanners not heard from for a while are shown offline. Run periodically."""
    from machine import registry

    from .machine import NfcScannerMachine as MachineType

    cutoff = timezone.now() - datetime.timedelta(seconds=offline_after_s)
    for machine in registry.get_machines(active=True):
        if not isinstance(machine, MachineType):
            continue
        seen = machine.last_seen
        if machine.status in (NfcScannerStatus.ONLINE, NfcScannerStatus.BUSY) and (seen is None or seen < cutoff):
            machine.set_status(NfcScannerStatus.OFFLINE)
            machine.set_status_text(str('not heard from since ' + (seen.strftime('%Y-%m-%d %H:%M:%S UTC') if seen else 'start')))
            # A job the scanner will not finish
            for job in Job.objects.filter(machine=machine.machine_config, state__in=[Job.State.SENT, Job.State.WAITING, Job.State.WRITING]):
                _finish(job, Job.State.FAILED, error='scanner_offline')
