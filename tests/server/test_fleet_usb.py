"""Updates over USB: the browser checks in for the scanner in hand, installs, and reports."""

import datetime

import pytest

from .releases import release, upload
from .scanner import P

READER = "nfc-0123456789ab"


@pytest.fixture
def browser(api, clerk):
    """The browser of a user who may program tags, with a scanner plugged in."""
    client = api(clerk)

    class Browser:
        def checkin(self, fw):
            r = client.post(
                f"{P}/api/usb/checkin/",
                {"reader": READER, "fw": fw, "proto": 1},
                format="json",
            )
            assert r.status_code == 200, r.json()
            return r.json()

        def post(self, dep, action, reader=READER, **body):
            return client.post(
                f"{P}/api/usb/deployments/{dep}/{action}/",
                {"reader": reader, **body},
                format="json",
            )

    return Browser()


@pytest.fixture
def offered(browser, fleet):
    """A scanner seen over USB on 1.0.0, and 1.1.0 deployed to it: `(deployment id, firmware)`."""

    def make(version="1.1.0", **deploy):
        browser.checkin("1.0.0")
        fw = upload(fleet, release(version)).json()
        r = fleet.post(
            f"{P}/api/fleet/deploy/",
            {"firmware": fw["id"], "scanners": [READER], **deploy},
            format="json",
        )
        return r.json()["results"][0]["deployment"], fw

    return make


def test_checking_in_registers_the_scanner_as_seen_over_usb(browser, fleet, clerk):
    assert browser.checkin("1.0.0")["update"] is None
    (sc,) = fleet.get(f"{P}/api/fleet/").json()["scanners"]
    assert sc["last_via"] == "usb" and sc["last_user"] == clerk.username


def test_a_user_who_may_not_program_tags_cannot_check_in(api, nobody):
    r = api(nobody).post(
        f"{P}/api/usb/checkin/", {"reader": READER, "fw": "1.0.0"}, format="json"
    )
    assert r.status_code == 403


def test_the_waiting_update_is_offered_deferrable_by_default(browser, offered):
    dep, fw = offered()
    update = browser.checkin("1.0.0")["update"]
    assert update["id"] == dep and not update["required"]
    assert update["url"].endswith("1.1.0/inventree_nfc_scanner-1.1.0.bin")
    assert update["sha256"] == fw["sha256"]


def test_the_user_may_put_it_off(browser, offered):
    dep, _ = offered()
    r = browser.post(dep, "defer")
    assert r.status_code == 200 and r.json()["deferrals"] == 1


def test_but_only_for_the_scanner_in_hand(browser, offered):
    dep, _ = offered()
    assert browser.post(dep, "defer", reader="nfc-000000000001").status_code == 403


def test_a_required_deployment_says_so_and_cannot_be_put_off(browser, offered):
    dep, _ = offered(required=True)
    assert browser.checkin("1.0.0")["update"]["required"]
    assert browser.post(dep, "defer").status_code == 400


def test_the_policy_setting_makes_every_update_required(browser, offered, set_setting):
    set_setting("FIRMWARE_USB_POLICY", "required")
    offered()
    assert browser.checkin("1.0.0")["update"]["required"]


def test_a_deployment_can_override_a_required_policy(browser, offered, set_setting):
    set_setting("FIRMWARE_USB_POLICY", "required")
    offered(required=False)
    assert not browser.checkin("1.0.0")["update"]["required"]


def test_required_from_a_date(browser, offered, time_machine):
    soon = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    offered(required_after=soon.isoformat())
    assert not browser.checkin("1.0.0")["update"]["required"]
    time_machine.move_to(soon + datetime.timedelta(minutes=1))
    assert browser.checkin("1.0.0")["update"]["required"]


def test_install_report_and_confirm(browser, offered, scanner):
    dep, fw = offered()
    r = browser.post(dep, "start")
    assert r.status_code == 200 and r.json()["sha256"] == fw["sha256"]
    assert browser.post(dep, "start").status_code == 400, "only once"
    for state in ("downloading", "restarting"):
        r = browser.post(dep, "report", state=state)
    assert r.json()["state"] == "restarting"
    ci = browser.checkin("1.1.0")
    assert ci["update"] is None and ci["last"]["state"] == "confirmed"


def test_an_update_being_installed_over_usb_is_not_also_sent_over_the_network(
    browser, offered, network_scanner, api, nobody
):
    from .scanner import FakeScanner

    m = network_scanner(reader=READER, user=nobody)
    net = FakeScanner(api(nobody), reader=READER, fw="1.0.0", machine=m)
    net.sync()
    dep, _ = offered()
    browser.checkin("1.0.0")
    browser.post(dep, "start")
    assert not [c for c in net.cmds() if c.get("cmd") == "ota"]


def test_a_failure_reported_by_the_browser_ends_it(browser, offered, fleet):
    dep, _ = offered()
    browser.post(dep, "start")
    r = browser.post(
        dep, "report", state="failed", error="bad_image", detail="sha256 mismatch"
    )
    assert r.json()["state"] == "failed"


def test_an_update_cut_off_is_offered_again_on_the_next_connection(browser, offered):
    dep, _ = offered()
    browser.post(dep, "start")
    browser.post(dep, "report", state="downloading")
    assert browser.checkin("1.0.0")["update"]["id"] == dep


def test_a_browser_that_went_away_mid_update_is_retried(
    browser, offered, time_machine, fleet
):
    from inventree_nfc_scanner.fleet import USB_TIMEOUT, check_deployments

    dep, _ = offered()
    browser.post(dep, "start")
    browser.post(dep, "report", state="downloading")
    time_machine.move_to(
        datetime.datetime.now(datetime.UTC)
        + USB_TIMEOUT
        + datetime.timedelta(minutes=1)
    )
    check_deployments()
    d = {x["id"]: x for x in fleet.get(f"{P}/api/fleet/deployments/").json()}[dep]
    assert d["state"] == "pending" and d["error"] == "interrupted"


def test_coming_back_on_the_old_version_over_usb_is_a_rollback(browser, offered, fleet):
    dep, _ = offered()
    browser.post(dep, "start")
    browser.post(dep, "report", state="restarting")
    browser.checkin("1.0.0")
    d = {x["id"]: x for x in fleet.get(f"{P}/api/fleet/deployments/").json()}[dep]
    assert d["state"] == "rolled_back"
