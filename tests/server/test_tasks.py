"""The every-minute task: scanners gone quiet, deployments gone quiet, and the plugin's settings."""

import datetime

import pytest

from .releases import release, upload
from .scanner import P


def later(time_machine, **delta):
    time_machine.move_to(
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(**delta)
    )


@pytest.fixture
def shared_cache(monkeypatch):
    """Pretend the cache is shared between processes (Redis), as offline detection needs."""
    from inventree_nfc_scanner import sync

    monkeypatch.setattr(sync, "shared_cache", lambda: True)


def scanner_state(api, clerk):
    (sc,) = api(clerk).get(f"{P}/api/scanners/").json()
    return sc


def test_a_quiet_scanner_is_shown_offline(
    scanner, shared_cache, api, clerk, plugin, time_machine
):
    scanner.sync()
    assert scanner_state(api, clerk)["online"]
    later(time_machine, seconds=41)
    plugin.check_scanners()
    sc = scanner_state(api, clerk)
    assert sc["status"] == "offline" and "not heard from since" in sc["status_text"]


def test_a_scanner_within_the_threshold_stays_online(
    scanner, shared_cache, api, clerk, plugin, time_machine
):
    scanner.sync()
    later(time_machine, seconds=30)
    plugin.check_scanners()
    assert scanner_state(api, clerk)["online"]


def test_the_threshold_is_never_under_the_hold(
    scanner, shared_cache, api, clerk, plugin, set_setting, time_machine
):
    set_setting("LONG_POLL", True)
    set_setting("LONG_POLL_MAX_S", 60)
    scanner.sync()
    later(time_machine, seconds=50)
    plugin.check_scanners()
    assert scanner_state(api, clerk)["online"]


def test_a_quiet_scanners_jobs_fail(
    scanner, shared_cache, job_for, api, clerk, plugin, time_machine
):
    scanner.sync()
    job = job_for(scanner)
    later(time_machine, minutes=5)
    plugin.check_scanners()
    j = api(clerk).get(f"{P}/api/jobs/{job['id']}/").json()
    assert j["state"] == "failed" and j["error"] == "scanner_offline"


def test_with_a_per_process_cache_nothing_is_marked_offline(
    scanner, api, clerk, plugin, time_machine
):
    scanner.sync()
    later(time_machine, minutes=5)
    plugin.check_scanners()
    assert scanner_state(api, clerk)["online"]


def test_old_bookkeeping_is_cleared(scanner, plugin, time_machine):
    from inventree_nfc_scanner.models import ScannerMessage

    scanner.sync({"evt": "tag", "uid": "04AABBCCDDEEFF"})
    later(time_machine, days=3)
    plugin.check_scanners()
    assert not ScannerMessage.objects.exists()


# Deployments gone quiet ----------------------------------------------------------------------


@pytest.fixture
def sent(scanner, fleet):
    """A network deployment sent to the scanner (on 1.0.0): its id."""
    scanner.fw = "1.0.0"
    scanner.sync()
    fw = upload(fleet, release("1.1.0")).json()
    dep = fleet.post(
        f"{P}/api/fleet/deploy/",
        {"firmware": fw["id"], "scanners": [scanner.reader]},
        format="json",
    ).json()
    scanner.cmds()
    return dep["results"][0]["deployment"]


def dep_state(fleet, dep):
    return {d["id"]: d for d in fleet.get(f"{P}/api/fleet/deployments/").json()}[dep]


def test_an_unanswered_update_is_tried_again_then_given_up(sent, fleet, time_machine):
    from inventree_nfc_scanner.fleet import (
        MAX_ATTEMPTS,
        SENT_TIMEOUT,
        check_deployments,
    )
    from inventree_nfc_scanner.models import Deployment

    later(time_machine, seconds=SENT_TIMEOUT.total_seconds() + 60)
    check_deployments()
    d = dep_state(fleet, sent)
    assert d["state"] == "pending" and d["error"] == "no_answer"
    Deployment.objects.filter(pk=sent).update(state="sent", attempts=MAX_ATTEMPTS)
    later(time_machine, seconds=SENT_TIMEOUT.total_seconds() + 60)
    check_deployments()
    d = dep_state(fleet, sent)
    assert d["state"] == "failed" and f"after {MAX_ATTEMPTS} attempts" in d["detail"]


def test_a_download_that_does_not_finish_fails(sent, scanner, fleet, time_machine):
    from inventree_nfc_scanner.fleet import DOWNLOAD_TIMEOUT, check_deployments

    scanner.sync({"evt": "ota", "state": "downloading"})
    later(time_machine, seconds=DOWNLOAD_TIMEOUT.total_seconds() + 60)
    check_deployments()
    d = dep_state(fleet, sent)
    assert d["state"] == "failed" and d["error"] == "timeout"


def test_a_scanner_that_does_not_return_after_restarting_fails(
    sent, scanner, fleet, time_machine
):
    from inventree_nfc_scanner.fleet import RESTART_TIMEOUT, check_deployments

    scanner.sync({"evt": "ota", "state": "restarting"})
    later(time_machine, seconds=RESTART_TIMEOUT.total_seconds() + 60)
    check_deployments()
    d = dep_state(fleet, sent)
    assert d["state"] == "failed" and d["error"] == "did_not_return"


# The settings' validators --------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, value",
    [
        ("TAG_PASSWORD", "xyz"),
        ("TAG_PASSWORD", "A1B2C3"),
        ("TAG_PACK", "12345"),
        ("JOB_TIMEOUT_S", "0"),
        ("JOB_TIMEOUT_S", "601"),
        ("LONG_POLL_MAX_S", "61"),
        ("SCANNER_OFFLINE_S", "5"),
        ("FIRMWARE_MAX_DOWNLOADS", "0"),
        ("FIRMWARE_USB_POLICY", "sometimes"),
    ],
)
def test_a_bad_setting_is_refused(api, admin_user, key, value):
    r = api(admin_user).patch(
        f"/api/plugins/nfcscanner/settings/{key}/", {"value": value}, format="json"
    )
    assert r.status_code == 400, (key, value, r.json())


@pytest.mark.parametrize(
    "key, value",
    [("TAG_PASSWORD", "a1b2c3d4"), ("TAG_PASSWORD", ""), ("JOB_TIMEOUT_S", "600")],
)
def test_a_good_setting_is_taken(api, admin_user, key, value):
    r = api(admin_user).patch(
        f"/api/plugins/nfcscanner/settings/{key}/", {"value": value}, format="json"
    )
    assert r.status_code == 200, r.json()


def test_the_password_settings_are_protected(api, admin_user, set_setting):
    set_setting("TAG_PASSWORD", "A1B2C3D4")
    r = api(admin_user).get("/api/plugins/nfcscanner/settings/TAG_PASSWORD/")
    assert "A1B2C3D4" not in str(r.json())
