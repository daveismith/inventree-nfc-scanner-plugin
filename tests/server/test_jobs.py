"""Jobs for network scanners: queued from the panel, carried by /sync, ended by the scanner."""

import datetime

import pytest

from .scanner import P

UID = "04A1B2C3D4E5F6"


@pytest.fixture
def jobs(api, clerk):
    """Read a job back as the panel does: `jobs(id)`."""
    client = api(clerk)
    return lambda pk: client.get(f"{P}/api/jobs/{pk}/").json()


def take(scanner, job):
    """The scanner collects the job's program command, accepts it and starts waiting."""
    (cmd,) = [c for c in scanner.cmds() if c.get("id") == job["id"]]
    scanner.ack(cmd)
    scanner.sync(
        {"rsp": "program", "ok": True, "id": job["id"]},
        {"evt": "waiting", "id": job["id"], "timeout_ms": 60000},
    )
    return cmd


def done(job, uid=UID, **extra):
    return {
        "evt": "done",
        "id": job["id"],
        "uid": uid,
        "type": "ntag215",
        "protected": False,
        **extra,
    }


# A job from start to finish ----------------------------------------------------------------


def test_a_job_start_to_finish(scanner, job_for, jobs, api, clerk, location):
    job = job_for(scanner)
    assert job["state"] == "queued"

    (cmd,) = scanner.cmds()
    assert (
        cmd["cmd"] == "program"
        and cmd["id"] == job["id"]
        and cmd["timeout_ms"] == 60000
    )
    assert cmd["ndef"].startswith("9101")
    assert jobs(job["id"])["state"] == "sent"

    scanner.ack(cmd)
    scanner.sync(
        {"rsp": "program", "ok": True, "id": job["id"]},
        {"evt": "waiting", "id": job["id"], "timeout_ms": 60000},
    )
    assert jobs(job["id"])["state"] == "waiting"
    (sc,) = api(clerk).get(f"{P}/api/scanners/").json()
    assert sc["status"] == "busy"

    scanner.sync({"evt": "writing", "id": job["id"], "uid": UID})
    j = jobs(job["id"])
    assert j["state"] == "writing" and j["uid"] == UID

    scanner.sync(done(job))
    j = jobs(job["id"])
    assert j["state"] == "done" and j["tag_type"] == "ntag215" and j["finished_at"]
    location.refresh_from_db()
    assert location.barcode_hash, "the UID is linked to the location as its barcode"


def test_the_command_carries_the_tag_password(scanner, job_for, set_setting):
    set_setting("TAG_PASSWORD", "A1B2C3D4")
    set_setting("TAG_PACK", "BEEF")
    job_for(scanner)
    (cmd,) = scanner.cmds()
    assert cmd["pwd"] == "A1B2C3D4" and cmd["pack"] == "BEEF"


def test_the_job_timeout_setting_is_carried(scanner, job_for, set_setting):
    set_setting("JOB_TIMEOUT_S", 15)
    job_for(scanner)
    assert scanner.cmds()[0]["timeout_ms"] == 15000


def test_the_overwrite_flag_is_carried(scanner, job_for):
    job_for(scanner, overwrite=True)
    assert scanner.cmds()[0]["overwrite"] is True


def test_a_refused_job_is_failed_with_what_the_tag_holds(scanner, job_for, jobs):
    job = job_for(scanner)
    take(scanner, job)
    scanner.sync({
        "evt": "failed",
        "id": job["id"],
        "error": "not_blank",
        "uid": UID,
        "text": "INV-SL9",
        "uri": "http://x/web/stock/location/9",
    })
    j = jobs(job["id"])
    assert (
        j["state"] == "failed"
        and j["error"] == "not_blank"
        and j["existing_text"] == "INV-SL9"
    )


def test_a_refused_program_command_ends_the_job(scanner, job_for, jobs):
    job = job_for(scanner)
    (cmd,) = scanner.cmds()
    scanner.ack(cmd)
    scanner.sync({"rsp": "program", "ok": False, "id": job["id"], "error": "nfc_error"})
    j = jobs(job["id"])
    assert j["state"] == "failed" and j["error"] == "nfc_error"


# Cancelling ----------------------------------------------------------------------------------


def test_a_queued_job_is_cancelled_before_the_scanner_sees_it(
    scanner, job_for, api, clerk
):
    job = job_for(scanner)
    j = api(clerk).post(f"{P}/api/jobs/{job['id']}/cancel/").json()
    assert j["state"] == "cancelled"
    assert scanner.cmds() == []


def test_a_collected_job_is_cancelled_by_asking_the_scanner(
    scanner, job_for, jobs, api, clerk
):
    job = job_for(scanner)
    take(scanner, job)
    api(clerk).post(f"{P}/api/jobs/{job['id']}/cancel/")
    (cancel,) = scanner.cmds()
    assert cancel["cmd"] == "cancel" and cancel["id"] == job["id"]

    scanner.ack(cancel)
    scanner.sync({"rsp": "cancel", "ok": False, "id": job["id"], "error": "busy"})
    assert jobs(job["id"])["state"] not in ("done", "failed", "cancelled"), (
        "too late is not the end"
    )

    scanner.sync({"evt": "failed", "id": job["id"], "error": "cancelled"})
    assert jobs(job["id"])["state"] == "cancelled"


def test_cancelling_twice_asks_once(scanner, job_for, api, clerk):
    job = job_for(scanner)
    take(scanner, job)
    api(clerk).post(f"{P}/api/jobs/{job['id']}/cancel/")
    api(clerk).post(f"{P}/api/jobs/{job['id']}/cancel/")
    assert [c["cmd"] for c in scanner.cmds()] == ["cancel"]


def test_cancelling_a_finished_job_changes_nothing(scanner, job_for, jobs, api, clerk):
    job = job_for(scanner)
    take(scanner, job)
    scanner.sync(done(job))
    assert (
        api(clerk).post(f"{P}/api/jobs/{job['id']}/cancel/").json()["state"] == "done"
    )


# One job at a time, and only believed events ------------------------------------------------


def test_a_second_job_for_a_busy_scanner_is_409(scanner, job_for, api, clerk, location):
    first = job_for(scanner)
    r = api(clerk).post(
        f"{P}/api/jobs/",
        {"location": location.pk, "scanner": str(scanner.machine.pk)},
        format="json",
    )
    assert r.status_code == 409 and r.json()["job"] == first["id"]


def test_events_for_a_job_not_yet_sent_are_ignored(scanner, job_for, jobs):
    job = job_for(scanner)
    # A done with this id before the command went out: a USB job's, say.
    scanner.sync(done(job, uid="04AABBCCDDEE01"), ack=0)
    j = jobs(job["id"])
    assert j["state"] == "sent" and j["uid"] == ""  # sent by this very call, not done


def test_once_sent_its_events_are_applied(scanner, job_for, jobs):
    job = job_for(scanner)
    take(scanner, job)
    scanner.sync({"evt": "failed", "id": job["id"], "error": "cancelled"})
    assert jobs(job["id"])["state"] == "cancelled"


def test_a_job_for_another_scanner_is_not_touched(
    scanner, network_scanner, api, nobody, job_for, jobs
):
    from .scanner import FakeScanner

    other_machine = network_scanner(user=nobody)
    other = FakeScanner(
        api(nobody),
        reader=other_machine.get_setting("READER_ID", "D"),
        machine=other_machine,
    )
    job = job_for(scanner)
    take(scanner, job)
    other.sync({"evt": "failed", "id": job["id"], "error": "cancelled"})
    assert jobs(job["id"])["state"] == "waiting"


# Restarts -----------------------------------------------------------------------------------


def test_a_restarted_scanner_is_not_sent_a_finished_job_again(scanner, job_for):
    job = job_for(scanner)
    scanner.restart()
    take(scanner, job)
    scanner.sync(done(job))
    scanner.restart()
    assert not any(c.get("id") == job["id"] for c in scanner.cmds())


def test_a_job_the_scanner_had_taken_fails_when_it_restarts(scanner, job_for, jobs):
    scanner.sync()  # its boot is known
    job = job_for(scanner)
    take(scanner, job)
    scanner.restart()
    scanner.sync()
    j = jobs(job["id"])
    assert j["state"] == "failed" and j["error"] == "scanner_restarted"


# Without the scanner's word ------------------------------------------------------------------


def test_a_job_with_no_result_expires(scanner, job_for, jobs, time_machine):
    from inventree_nfc_scanner.sync import RESULT_GRACE_S, expire_jobs

    job = job_for(scanner)
    take(scanner, job)
    time_machine.move_to(
        datetime.datetime.now(datetime.UTC)
        + datetime.timedelta(seconds=60 + RESULT_GRACE_S + 5)
    )
    expire_jobs()
    j = jobs(job["id"])
    assert j["state"] == "failed" and j["error"] == "no_result"


def test_a_job_within_its_time_does_not_expire(scanner, job_for, jobs):
    from inventree_nfc_scanner.sync import expire_jobs

    job = job_for(scanner)
    take(scanner, job)
    expire_jobs()
    assert jobs(job["id"])["state"] == "waiting"


# A late done (check_rules.py) ----------------------------------------------------------------


@pytest.fixture
def sent_job(scanner, location, clerk):
    """`sent_job(**fields)`: a job row whose program command went to the scanner."""
    from django.utils import timezone

    from inventree_nfc_scanner.models import Job, ScannerCommand

    config = scanner.machine.machine_config

    def make(**fields):
        job = Job.objects.create(
            location=location, machine=config, created_by=clerk, **fields
        )
        ScannerCommand.objects.create(
            machine=config,
            seq=900000 + job.pk,
            job=job,
            payload={"cmd": "program", "id": job.pk},
            sent_at=timezone.now(),
        )
        return job

    return make


@pytest.mark.parametrize("error", ["scanner_offline", "no_result"])
def test_a_done_soon_after_giving_up_is_believed(sent_job, error):
    from django.utils import timezone

    from inventree_nfc_scanner.sync import _late_done_believed

    job = sent_job(
        state="failed",
        error=error,
        finished_at=timezone.now() - datetime.timedelta(minutes=5),
    )
    assert _late_done_believed(job)


def test_a_done_long_after_giving_up_is_not(sent_job):
    from django.utils import timezone

    from inventree_nfc_scanner.sync import LATE_DONE_WINDOW, _late_done_believed

    job = sent_job(
        state="failed",
        error="no_result",
        finished_at=timezone.now() - LATE_DONE_WINDOW - datetime.timedelta(minutes=1),
    )
    assert not _late_done_believed(job)


def test_a_late_done_is_not_believed_once_the_bin_has_a_newer_job(sent_job):
    from django.utils import timezone

    from inventree_nfc_scanner.sync import _late_done_believed

    job = sent_job(state="failed", error="no_result", finished_at=timezone.now())
    sent_job(state="queued")
    assert not _late_done_believed(job)


def test_a_late_done_is_never_believed_for_a_failure_of_its_own(sent_job):
    from django.utils import timezone

    from inventree_nfc_scanner.sync import _late_done_believed

    assert not _late_done_believed(
        sent_job(state="failed", error="tag_removed", finished_at=timezone.now())
    )


def test_a_believed_late_done_finishes_the_job_and_links(
    scanner, sent_job, location, jobs
):
    from django.utils import timezone

    job = sent_job(state="failed", error="scanner_offline", finished_at=timezone.now())
    scanner.sync(done({"id": job.pk}))
    assert jobs(job.pk)["state"] == "done"
    location.refresh_from_db()
    assert location.barcode_hash


# Refusals at creation ------------------------------------------------------------------------


def test_a_job_needs_change_permission(scanner, api, nobody, location):
    r = api(nobody).post(
        f"{P}/api/jobs/",
        {"location": location.pk, "scanner": str(scanner.machine.pk)},
        format="json",
    )
    assert r.status_code == 403


def test_a_job_needs_the_base_url(scanner, api, clerk, location):
    from common.models import InvenTreeSetting

    InvenTreeSetting.set_setting("INVENTREE_BASE_URL", "", None)
    r = api(clerk).post(
        f"{P}/api/jobs/",
        {"location": location.pk, "scanner": str(scanner.machine.pk)},
        format="json",
    )
    assert r.status_code == 400 and "base_url" in r.json()
    from inventree_nfc_scanner.models import Job

    assert not Job.objects.exists(), "nothing is left behind"


def test_a_job_for_an_unknown_scanner_is_400(machines, api, clerk, location):
    import uuid

    r = api(clerk).post(
        f"{P}/api/jobs/",
        {"location": location.pk, "scanner": str(uuid.uuid4())},
        format="json",
    )
    assert r.status_code == 400


# The USB route's record ----------------------------------------------------------------------


def test_a_usb_job_is_recorded_with_no_scanner(api, clerk, location):
    r = api(clerk).post(
        f"{P}/api/location/{location.pk}/jobs/usb/",
        {"state": "done", "uid": UID, "tag_type": "ntag215", "protected": True},
        format="json",
    )
    assert r.status_code == 201
    j = r.json()
    assert j["scanner"] is None and j["state"] == "done"
