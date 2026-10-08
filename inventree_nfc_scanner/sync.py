"""The exchange with a network scanner: one `/sync` call in, one answer out.

The contract is in docs/api.md. The rules that matter:

- Every command the server sends is numbered and sent again on every call until the
  scanner's `ack` covers it. Every message the scanner sends is numbered too; one the server
  has already seen (same reader, boot, seq) is acknowledged and not applied again.
- A `program` job's id on the wire is the Job's primary key, so each event finds its row.
- A `tag` event outside a job is the scanner's last tap, kept in the machine's state.
- Everything a scanner sends is bounded and typed before it is stored; a malformed body is
  refused with 400, and a message that still cannot be applied is logged and dropped.
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

from machine.models import MachineConfig
from rest_framework.exceptions import ValidationError

from .barcodes import UID_RE
from .machine import NfcScannerMachine, NfcScannerStatus
from .models import Job, ScannerCommand, ScannerMessage

logger = logging.getLogger("inventree")

PROTO_VERSION = 1
POLL_STEP_S = 0.25
SEQ_MAX = 2**31 - 1
KEEP_BOOKKEEPING_FOR = datetime.timedelta(
    days=2
)  # acknowledged commands and seen messages
SECRET_FIELDS = ("pwd", "pack")

# The scanner's error codes that mean "the tag is not blank and I was not told to overwrite".
NOT_BLANK = "not_blank"


# Everything a scanner sends is bounded and typed before it touches a row: a hostile or
# broken scanner must not be able to raise a database error, which would stall its job.


def _text(value, max_len: int) -> str:
    return value[:max_len] if isinstance(value, str) else ""


def _uid(value) -> str:
    uid = value.strip().upper() if isinstance(value, str) else ""
    return uid if UID_RE.match(uid) else ""


def _flag(value):
    return value if isinstance(value, bool) else None


def _seq(value) -> int | None:
    """A whole number from 0 to 2^31-1: a JSON int, or a float with nothing after the point."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) and 0 <= value <= SEQ_MAX else None


def enqueue(machine_config, payload: dict, job: Job | None = None) -> ScannerCommand:
    """Queue a command for a scanner, with the next sequence number."""
    with transaction.atomic():
        # Two queues for the same scanner at once must not pick the same number.
        MachineConfig.objects.select_for_update().get(pk=machine_config.pk)
        last = (
            ScannerCommand.objects.filter(machine=machine_config).aggregate(
                m=Max("seq")
            )["m"]
            or 0
        )
        # The newest command is never pruned (see mark_stale_scanners), so the numbering
        # never goes back below what the scanner has acknowledged.
        return ScannerCommand.objects.create(
            machine=machine_config, seq=last + 1, job=job, payload=payload
        )


def pending_commands(machine_config):
    """Commands not yet acknowledged, oldest first; never one for a job that has already ended."""
    return (
        ScannerCommand.objects
        .filter(machine=machine_config, acked_at__isnull=True)
        .exclude(job__state__in=[Job.State.DONE, Job.State.FAILED, Job.State.CANCELLED])
        .order_by("seq")
    )


def _finish(job: Job, state: str, **fields) -> None:
    for key, value in fields.items():
        setattr(job, key, value)
    job.state = state
    job.finished_at = timezone.now()
    job.save()
    # Its commands are done with. A scanner that restarts begins its acks again at zero, so
    # anything still marked unacknowledged would be sent again, and a finished `program`
    # must not be written twice.
    _retire(ScannerCommand.objects.filter(job=job, acked_at__isnull=True))


def _link_barcode(job: Job) -> None:
    """Make the tag's UID the location's barcode, on behalf of whoever asked for the job."""
    from .barcodes import link_uid

    if not job.uid:
        return
    try:
        outcome = link_uid(job.location, job.uid, job.created_by)
        if outcome.startswith("moved"):
            job.error_detail = f"barcode {outcome}"[:200]
            job.save()
    except Exception as exc:  # noqa: BLE001 - the job succeeded; the link is reported, not fatal
        logger.warning(
            "NFC job %s: could not link UID %s to %s: %s",
            job.pk,
            job.uid,
            job.location,
            exc,
        )
        job.error_detail = f"tag written; barcode link failed: {exc}"[:200]
        job.save()


def apply_message(machine: NfcScannerMachine, msg: dict) -> None:
    """Act on one message from the scanner."""
    job_id = _seq(msg.get("id"))
    job = None
    if job_id is not None:
        job = Job.objects.filter(pk=job_id, machine=machine.machine_config).first()

    if "rsp" in msg:
        # The answer to a command. Only a refused `program` or `wipe` ends its job; a refused
        # `cancel` (too late: the tag is being written) must not, since `done` follows.
        if (
            job
            and msg.get("rsp") in ("program", "wipe")
            and msg.get("ok") is not True
            and not job.finished
        ):
            _finish(
                job,
                Job.State.FAILED,
                error=_text(msg.get("error"), 32) or "refused",
                error_detail=_text(msg.get("detail"), 200),
            )
        return

    evt = msg.get("evt")
    if evt == "tag":
        machine.set_last_tag({
            "uid": _uid(msg.get("uid")) or None,
            "type": _text(msg.get("type"), 12) or None,
            "text": _text(msg.get("text"), 128) or None,
            "uri": _text(msg.get("uri"), 256) or None,
            "protected": _flag(msg.get("protected")),
            "error": _text(msg.get("error"), 32) or None,
            "at": timezone.now().isoformat(),
        })
        return

    if job is None or job.finished:
        return

    if evt == "waiting":
        job.state = Job.State.WAITING
        job.save()
        machine.set_status(NfcScannerStatus.BUSY)
    elif evt == "writing":
        job.state = Job.State.WRITING
        job.uid = _uid(msg.get("uid")) or job.uid
        job.save()
    elif evt == "done":
        _finish(
            job,
            Job.State.DONE,
            uid=_uid(msg.get("uid")) or job.uid,
            tag_type=_text(msg.get("type"), 12),
            protected=_flag(msg.get("protected")),
        )
        if job.kind == Job.Kind.PROGRAM:
            _link_barcode(job)
        machine.set_status(NfcScannerStatus.ONLINE)
    elif evt == "failed":
        error = _text(msg.get("error"), 32) or "failed"
        state = Job.State.CANCELLED if error == "cancelled" else Job.State.FAILED
        _finish(
            job,
            state,
            error=error,
            error_detail=_text(msg.get("detail"), 200),
            uid=_uid(msg.get("uid")) or job.uid,
            existing_text=_text(msg.get("text"), 128) if error == NOT_BLANK else "",
            existing_uri=_text(msg.get("uri"), 256) if error == NOT_BLANK else "",
        )
        machine.set_status(NfcScannerStatus.ONLINE)


def _retire(commands) -> None:
    """Commands that are done with: acknowledged, and the secrets they carried not kept. One
    deleted meanwhile (a cancel racing this) is simply gone."""
    now = timezone.now()
    for command in list(commands):
        payload = {k: v for k, v in command.payload.items() if k not in SECRET_FIELDS}
        ScannerCommand.objects.filter(pk=command.pk).update(
            payload=payload, acked_at=now
        )


def handle_sync(
    machine: NfcScannerMachine, body: dict, *, long_poll_max_s: int
) -> dict:
    """Process one /sync call and build its answer. Raises ValidationError for a body that is
    not what a scanner sends."""
    config = machine.machine_config
    boot = _seq(body.get("boot", 0))
    ack = _seq(body.get("ack", 0))
    wait = _seq(body.get("wait_s", 0) or 0)
    msgs = body.get("msgs") or []
    if boot is None or ack is None or wait is None:
        raise ValidationError({
            "detail": "boot, ack and wait_s must be whole numbers from 0 to 2^31-1"
        })
    if not isinstance(msgs, list) or not all(isinstance(m, dict) for m in msgs):
        raise ValidationError({"msgs": "expected a list of objects"})
    wait_s = min(wait, long_poll_max_s)

    machine.touch(boot)
    if machine.status in (NfcScannerStatus.OFFLINE, NfcScannerStatus.UNKNOWN):
        machine.set_status(NfcScannerStatus.ONLINE)
    machine.set_status_text(
        str(timezone.now().strftime("last seen %Y-%m-%d %H:%M:%S UTC"))
    )

    # 1. What the scanner has acted on is done with.
    _retire(
        ScannerCommand.objects.filter(
            machine=config, acked_at__isnull=True, seq__lte=ack
        )
    )

    # 2. Apply what it reports, once.
    highest = 0
    for msg in msgs:
        seq = _seq(msg.get("seq"))
        if seq is None:
            continue
        highest = max(highest, seq)
        _, fresh = ScannerMessage.objects.get_or_create(
            machine=config, boot=boot, seq=seq
        )
        if fresh:
            try:
                apply_message(machine, msg)
            except Exception:  # one bad message must not stall the exchange
                logger.exception(
                    "NFC scanner %s: message %s not applied", config.pk, msg
                )

    # 3. Hand out what is waiting; hold the call for more if asked and allowed.
    deadline = time.monotonic() + wait_s
    while True:
        commands = list(pending_commands(config))
        if commands or time.monotonic() >= deadline:
            break
        time.sleep(POLL_STEP_S)

    # Marked sent under a lock, so that a cancel arriving at the same moment sees one state
    # or the other, never a command it deleted being handed out.
    sent_at = timezone.now()
    with transaction.atomic():
        # The job rows first, then the commands: the same order as a cancel, so the two
        # cannot wait on each other. A command retired or cancelled meanwhile is left out.
        list(
            Job.objects.select_for_update().filter(
                pk__in=[c.job_id for c in commands if c.job_id]
            )
        )
        # No join under the lock: Postgres cannot lock the nullable side of one.
        ended = set(
            Job.objects.filter(
                pk__in=[c.job_id for c in commands if c.job_id],
                state__in=[Job.State.DONE, Job.State.FAILED, Job.State.CANCELLED],
            ).values_list("pk", flat=True)
        )
        commands = [
            c
            for c in ScannerCommand.objects
            .select_for_update()
            .filter(pk__in=[c.pk for c in commands], acked_at__isnull=True)
            .order_by("seq")
            if c.job_id not in ended
        ]
        for command in commands:
            if command.sent_at is None:
                command.sent_at = sent_at
                command.save(update_fields=["sent_at"])
                Job.objects.filter(pk=command.job_id, state=Job.State.QUEUED).update(
                    state=Job.State.SENT, updated_at=sent_at
                )

    return {
        "ack": highest,
        "cmds": [dict(command.payload, seq=command.seq) for command in commands],
    }


def mark_stale_scanners(offline_after_s: int) -> None:
    """Scanners not heard from for a while are shown offline. Run periodically."""
    from machine import registry

    from .machine import NfcScannerMachine as MachineType

    # Bookkeeping that has served its purpose goes, so the tables do not grow for ever. The
    # newest command of each scanner stays, whatever its age: the next number comes from it.
    old = timezone.now() - KEEP_BOOKKEEPING_FOR
    newest = (
        ScannerCommand.objects
        .order_by("machine_id", "-seq")
        .distinct("machine_id")
        .values_list("pk", flat=True)
    )
    ScannerCommand.objects.filter(acked_at__lt=old).exclude(
        pk__in=list(newest)
    ).delete()
    ScannerMessage.objects.filter(received_at__lt=old).delete()

    # A scanner in a held call has not gone quiet: the threshold is never under the hold.
    from plugin import registry as plugin_registry

    plg = plugin_registry.get_plugin("nfcscanner")
    hold_s = (
        int(plg.get_setting("LONG_POLL_MAX_S") or 0)
        if plg and plg.get_setting("LONG_POLL")
        else 0
    )
    offline_after_s = max(offline_after_s, hold_s + 15)

    cutoff = timezone.now() - datetime.timedelta(seconds=offline_after_s)
    for machine in registry.get_machines(active=True):
        if not isinstance(machine, MachineType):
            continue
        seen = machine.last_seen
        if machine.status in (NfcScannerStatus.ONLINE, NfcScannerStatus.BUSY) and (
            seen is None or seen < cutoff
        ):
            machine.set_status(NfcScannerStatus.OFFLINE)
            machine.set_status_text(
                str(
                    "not heard from since "
                    + (seen.strftime("%Y-%m-%d %H:%M:%S UTC") if seen else "start")
                )
            )
            # A job the scanner will not finish, and one it will not even collect
            for job in Job.objects.filter(
                machine=machine.machine_config,
                state__in=[
                    Job.State.QUEUED,
                    Job.State.SENT,
                    Job.State.WAITING,
                    Job.State.WRITING,
                ],
            ):
                _finish(job, Job.State.FAILED, error="scanner_offline")
