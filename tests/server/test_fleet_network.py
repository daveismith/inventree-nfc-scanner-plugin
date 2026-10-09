"""Deploying firmware to a network scanner, through /sync, step by step."""

import pytest

from .releases import release, upload
from .scanner import P

OLD = "1.0.0"


@pytest.fixture
def releases(fleet):
    """Upload releases by version: `releases("1.1.0", ...)` → {version: firmware dict}."""

    def make(*versions, **kw):
        out = {}
        for v in versions:
            rel = release(v, **kw)
            out[v] = dict(upload(fleet, rel).json(), release=rel)
        return out

    return make


@pytest.fixture
def deploy(fleet, scanner):
    """Deploy to the scanner: returns the response; `.dep` is the deployment id, if one was made."""

    def go(fw, **kw):
        body = {"firmware": fw["id"], "scanners": [scanner.reader], **kw}
        r = fleet.post(f"{P}/api/fleet/deploy/", body, format="json")
        r.dep = (
            r.json()["results"][0].get("deployment") if r.status_code == 200 else None
        )
        return r

    return go


@pytest.fixture
def deps(fleet, scanner):
    return lambda: {
        d["id"]: d
        for d in fleet.get(
            f"{P}/api/fleet/deployments/?scanner={scanner.reader}"
        ).json()
    }


@pytest.fixture
def seen(scanner):
    """The scanner, on OLD, known to the server."""
    scanner.fw = OLD
    scanner.sync()
    return scanner


def otas(cmds):
    return [c for c in cmds if c.get("cmd") == "ota"]


def run_ota(scanner, dep_id, *, to=None):
    """The scanner takes the ota command, downloads and restarts, coming back on `to`."""
    (cmd,) = otas(scanner.cmds())
    scanner.ack(cmd)
    scanner.sync(
        {"rsp": "ota", "ok": True, "id": dep_id},
        {"evt": "ota", "state": "downloading"},
        {"evt": "ota", "state": "restarting"},
    )
    scanner.restart()
    if to:
        scanner.fw = to
    scanner.sync()
    return cmd


# What is refused ------------------------------------------------------------------------------


def test_an_incompatible_release_cannot_be_deployed(seen, releases, deploy):
    fw = releases("1.3.0", proto=2)["1.3.0"]
    r = deploy(fw)
    assert r.status_code == 400 and "protocol" in str(r.json())


def test_nor_one_the_scanner_runs_already(seen, releases, deploy):
    r = deploy(releases(OLD)[OLD])
    assert (
        r.status_code == 200
        and f"runs {OLD} already" in r.json()["results"][0]["refused"]
    )


def test_an_unknown_scanner_is_refused(seen, releases, fleet):
    fw = releases("1.1.0")["1.1.0"]
    r = fleet.post(
        f"{P}/api/fleet/deploy/",
        {"firmware": fw["id"], "scanners": ["nfc-nope"]},
        format="json",
    )
    assert r.status_code == 400 and "unknown" in str(r.json())


@pytest.mark.parametrize(
    "body", [{"scanners": "some"}, {"required": "yes"}, {"required_after": "tomorrow"}]
)
def test_a_malformed_deploy_is_400(seen, releases, fleet, body):
    fw = releases("1.1.0")["1.1.0"]
    r = fleet.post(
        f"{P}/api/fleet/deploy/",
        {"firmware": fw["id"], "scanners": [seen.reader], **body},
        format="json",
    )
    assert r.status_code == 400


def test_a_downgrade_needs_confirming(seen, releases, deploy):
    seen.fw = "1.2.0"
    seen.sync()
    fw = releases("1.1.0")["1.1.0"]
    assert "older" in deploy(fw).json()["results"][0]["refused"]
    assert deploy(fw, allow_downgrade=True).dep


def test_deploying_to_all(seen, releases, fleet, network_scanner, api, nobody):
    from .scanner import FakeScanner

    other = network_scanner(user=nobody)
    FakeScanner(api(nobody), reader=other.get_setting("READER_ID", "D"), fw=OLD).sync()
    fw = releases("1.1.0")["1.1.0"]
    r = fleet.post(
        f"{P}/api/fleet/deploy/",
        {"firmware": fw["id"], "scanners": "all"},
        format="json",
    )
    assert len([x for x in r.json()["results"] if x.get("deployment")]) == 2


# The route ------------------------------------------------------------------------------------


def test_a_second_deployment_supersedes_one_still_pending(seen, releases, deploy, deps):
    fws = releases("1.1.0", "1.2.0")
    first, second = deploy(fws["1.1.0"]).dep, deploy(fws["1.2.0"]).dep
    d = deps()
    assert d[first]["state"] == "superseded" and d[second]["state"] == "pending"


def test_the_ota_command_names_the_image_on_this_server(seen, releases, deploy):
    fw = releases("1.1.0")["1.1.0"]
    dep = deploy(fw).dep
    (cmd,) = otas(seen.cmds())
    rel = fw["release"]
    assert cmd["id"] == dep and cmd["sha256"] == rel.sha256
    assert cmd["url"] == f"http://inventree.test{P}/firmware/1.1.0/{rel.app_file}"


def test_while_a_job_is_unfinished_the_update_waits(seen, releases, deploy, job_for):
    job = job_for(seen)
    deploy(releases("1.1.0")["1.1.0"])
    cmds = seen.cmds()
    assert not otas(cmds) and [c["cmd"] for c in cmds] == ["program"]
    seen.ack(cmds[0])
    seen.sync(
        {"rsp": "program", "ok": True, "id": job["id"]},
        {"evt": "failed", "id": job["id"], "error": "cancelled"},
    )
    assert otas(seen.cmds())


def test_no_job_can_be_queued_while_it_updates(
    seen, releases, deploy, api, clerk, location
):
    deploy(releases("1.1.0")["1.1.0"])
    seen.cmds()
    r = api(clerk).post(
        f"{P}/api/jobs/",
        {"location": location.pk, "scanner": str(seen.machine.pk)},
        format="json",
    )
    assert r.status_code == 400 and "updating" in str(r.json())


def test_a_busy_scanner_puts_it_back_without_counting_an_attempt(
    seen, releases, deploy, deps
):
    dep = deploy(releases("1.1.0")["1.1.0"]).dep
    (cmd,) = otas(seen.cmds())
    seen.ack(cmd)
    seen.sync({
        "rsp": "ota",
        "ok": False,
        "id": dep,
        "error": "busy",
        "detail": "a job is running",
    })
    d = deps()[dep]
    assert d["state"] == "pending" and d["error"] == "busy" and d["attempts"] == 0
    assert not otas(seen.cmds()), "and it is not sent again at once"


def test_progress_moves_it_to_downloading(seen, releases, deploy, deps):
    dep = deploy(releases("1.1.0")["1.1.0"]).dep
    (cmd,) = otas(seen.cmds())
    seen.ack(cmd)
    seen.sync(
        {"rsp": "ota", "ok": True, "id": dep}, {"evt": "ota", "state": "downloading"}
    )
    d = deps()[dep]
    assert (
        d["state"] == "downloading"
        and d["via"] == "network"
        and d["from_version"] == OLD
    )


def test_a_restart_mid_download_is_an_interrupted_attempt(seen, releases, deploy, deps):
    dep = deploy(releases("1.1.0")["1.1.0"]).dep
    (cmd,) = otas(seen.cmds())
    seen.ack(cmd)
    seen.sync(
        {"rsp": "ota", "ok": True, "id": dep}, {"evt": "ota", "state": "downloading"}
    )
    seen.restart()
    seen.sync()
    d = deps()[dep]
    assert (
        d["state"] in ("pending", "sent")
        and d["error"] in ("interrupted", "")
        and d["attempts"] >= 1
    )


def test_coming_back_on_the_new_version_confirms_it(
    seen, releases, deploy, deps, fleet
):
    dep = deploy(releases("1.1.0")["1.1.0"]).dep
    run_ota(seen, dep, to="1.1.0")
    d = deps()[dep]
    assert d["state"] == "confirmed" and d["finished_at"]
    (sc,) = fleet.get(f"{P}/api/fleet/").json()["scanners"]
    assert sc["fw"] == "1.1.0" and sc["outdated"] is False


def test_coming_back_on_the_old_version_is_a_rollback(seen, releases, deploy, deps):
    dep = deploy(releases("1.1.0")["1.1.0"]).dep
    run_ota(seen, dep, to=OLD)
    d = deps()[dep]
    assert d["state"] == "rolled_back" and OLD in d["detail"]


def test_a_failure_the_scanner_reports_ends_it(seen, releases, deploy, deps):
    dep = deploy(releases("1.1.0")["1.1.0"]).dep
    (cmd,) = otas(seen.cmds())
    seen.ack(cmd)
    seen.sync({
        "evt": "ota",
        "state": "failed",
        "error": "bad_image",
        "detail": "sha256 mismatch",
    })
    d = deps()[dep]
    assert d["state"] == "failed" and d["error"] == "bad_image"


def test_an_admin_can_withdraw_a_pending_deployment(seen, releases, deploy, fleet):
    dep = deploy(releases("1.1.0")["1.1.0"]).dep
    r = fleet.post(f"{P}/api/fleet/deployments/{dep}/cancel/")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert not otas(seen.cmds())


def test_a_scanner_mid_update_cannot_be_forgotten(seen, releases, deploy, fleet):
    deploy(releases("1.1.0")["1.1.0"])
    seen.cmds()
    assert fleet.delete(f"{P}/api/fleet/scanners/{seen.reader}/").status_code == 400


def test_downloads_at_once_are_limited(
    releases, fleet, network_scanner, api, nobody, set_setting
):
    from django.contrib.auth.models import User

    from .scanner import FakeScanner

    set_setting("FIRMWARE_MAX_DOWNLOADS", 1)
    fw = releases("1.1.0")["1.1.0"]
    scanners = []
    for _ in range(2):
        user = User.objects.create_user(username=f"scanner-user-{len(scanners)}")
        m = network_scanner(user=user)
        s = FakeScanner(
            api(user), reader=m.get_setting("READER_ID", "D"), fw=OLD, machine=m
        )
        s.sync()
        scanners.append(s)
    fleet.post(
        f"{P}/api/fleet/deploy/",
        {"firmware": fw["id"], "scanners": "all"},
        format="json",
    )
    assert [len(otas(s.cmds())) for s in scanners] == [1, 0]


def test_whoever_asked_is_told_when_an_update_does_not_take(
    seen, releases, deploy, notifications, admin_user
):
    dep = deploy(releases("1.1.0")["1.1.0"]).dep
    run_ota(seen, dep, to=OLD)
    ((category, targets, context),) = notifications
    assert category == "nfcscanner.firmware_failed" and targets == [admin_user]
    assert seen.reader in context["message"] and "rolled back" in context["message"]


def test_nobody_is_told_when_it_works(seen, releases, deploy, notifications):
    run_ota(seen, deploy(releases("1.1.0")["1.1.0"]).dep, to="1.1.0")
    assert notifications == []
