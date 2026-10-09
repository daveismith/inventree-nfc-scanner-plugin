"""Checks of server rules that need database state HTTP cannot set up, run inside the dev

server in a transaction that is rolled back, so nothing it makes is kept or seen by a scanner:

    docker exec -i nfcdev-server sh -c 'cd /home/inventree/src/backend/InvenTree && python manage.py shell' < check_rules.py

A stand-in for the Django test suite the plugin does not have yet (docs/open-issues.md, "No automated tests of the server logic").
"""

import datetime
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from stock.models import StockLocation
from inventree_nfc_scanner.models import Job, ScannerCommand
from inventree_nfc_scanner import sync, barcodes


class Rollback(Exception):
    pass


results = []


def check(cond, what):
    results.append(("ok  " if cond else "FAIL") + " " + what)


try:
    with transaction.atomic():
        from machine.models import MachineConfig
        from inventree_nfc_scanner.machine import NfcScannerStatus

        cfg = MachineConfig.objects.get(name="Desk scanner")

        class StandIn:
            """What handle_sync needs of a machine, without the machine registry."""

            machine_config = cfg
            status = NfcScannerStatus.ONLINE

            def touch(self, boot):
                pass

            def set_status(self, s):
                pass

            def set_status_text(self, t):
                pass

            def set_last_tag(self, t):
                pass

        machine = StandIn()
        admin = User.objects.get(username="admin")
        loc = StockLocation.objects.create(name="rules-check")

        def sent_job(**kw):
            j = Job.objects.create(location=loc, machine=cfg, created_by=admin, **kw)
            ScannerCommand.objects.create(
                machine=cfg,
                seq=900000 + j.pk,
                job=j,
                payload={"cmd": "program", "id": j.pk},
                sent_at=timezone.now(),
            )
            return j

        now = timezone.now()
        a = sent_job(
            state="failed",
            error="scanner_offline",
            finished_at=now - datetime.timedelta(minutes=5),
        )
        check(
            sync._late_done_believed(a),
            "a done five minutes after giving up is believed",
        )
        a.finished_at = now - datetime.timedelta(minutes=45)
        a.save()
        check(
            not sync._late_done_believed(a), "a done 45 minutes after giving up is not"
        )
        b = sent_job(
            state="failed",
            error="no_result",
            finished_at=now - datetime.timedelta(minutes=5),
        )
        check(sync._late_done_believed(b), "the same for a job given up as no_result")
        c = sent_job(state="queued")
        check(
            not sync._late_done_believed(b), "but not once the bin has had a newer job"
        )
        d = sent_job(state="failed", error="tag_removed", finished_at=now)
        check(
            not sync._late_done_believed(d),
            "and never for a job that failed for a reason of its own",
        )

        u = User.objects.create(username="rules-check-user", is_active=True)
        try:
            barcodes.link_uid(loc, "04AABBCCDDEE77", u)
            check(
                False,
                "a user without change permission cannot link (late done on their behalf)",
            )
        except barcodes.NotPermitted:
            check(
                True,
                "a user without change permission cannot link (late done on their behalf)",
            )

        Job.objects.filter(machine=cfg).exclude(state__in=Job.FINISHED).update(
            state="cancelled"
        )
        for i in range(4):
            ScannerCommand.objects.create(
                machine=cfg, seq=990000 + i, payload={"cmd": "info", "id": 7000 + i}
            )
        out = sync.handle_sync(
            machine,
            {"reader": "nfc-34b7da52a084", "boot": 1, "proto": 1, "ack": 0, "msgs": []},
            long_poll_max_s=0,
        )
        check(
            len(out["cmds"]) == 2,
            f"an answer carries at most two commands ({len(out['cmds'])} of 4 pending)",
        )
        raise Rollback()
except Rollback:
    pass
print("\n".join(results))
