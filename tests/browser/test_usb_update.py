"""Firmware updates over USB, as the browser installs them (scanner.ts, UpdateOverlay.tsx)."""

import re

import pytest
from playwright.sync_api import expect

from ..firmware_release import release
from .conftest import (
    deploy,
    registered,
    deployment,
    open_panel,
    replug,
    unplug,
    upload_release,
    usb_badge,
    version,
)


def offered(page, api, usb, location, **deploy_kw):
    """The scanner seen over USB, then a release deployed to it, then the page again."""
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    registered(api, usb.reader)
    rel = release(version())
    fw = upload_release(api, rel)
    (dep,) = deploy(api, fw, [usb.reader], **deploy_kw)
    usb.next_fw = rel.version
    page.reload()
    return rel, dep


def test_later_puts_it_off(admin_page, usb, location, api):
    page = admin_page
    rel, dep = offered(page, api, usb, location)
    expect(page.get_by_text(f"Firmware {rel.version} for this scanner")).to_be_visible()
    page.get_by_role("button", name="Later").click()
    expect(
        page.get_by_text(
            f"Firmware {rel.version} will be offered again next time the scanner connects."
        )
    ).to_be_visible()
    assert deployment(api, dep)["deferrals"] == 1
    assert not usb.commands("ota_begin")


def test_update_now_installs_it(admin_page, usb, location, api):
    page = admin_page
    rel, dep = offered(page, api, usb, location)
    page.get_by_role("button", name="Update now").click()
    # Streamed, the scanner restarts (gone from USB, back), the page reconnects by itself and
    # the check-in that follows confirms it.
    expect(
        page.get_by_text(f"Scanner updated to firmware {rel.version}.")
    ).to_be_visible()
    assert usb.restarts == 1 and usb.fw == rel.version
    assert deployment(api, dep)["state"] == "confirmed"
    (begin,) = usb.commands("ota_begin")
    assert begin["size"] == len(rel.app) and begin["sha256"] == rel.sha256
    expect(usb_badge(page)).to_have_text("connected")


def test_a_required_update_installs_at_once(admin_page, usb, location, api):
    page = admin_page
    rel, dep = offered(page, api, usb, location, required=True)
    expect(
        page.get_by_text(f"Scanner updated to firmware {rel.version}.")
    ).to_be_visible()
    expect(page.get_by_role("button", name="Later")).to_have_count(0)
    assert deployment(api, dep)["state"] == "confirmed"


def test_an_image_the_scanner_refuses(admin_page, usb, location, api):
    usb.refuse["ota_end"] = "bad_image"
    page = admin_page
    rel, dep = offered(page, api, usb, location)
    page.get_by_role("button", name="Update now").click()
    expect(
        page.get_by_text(re.compile("The firmware update did not finish"))
    ).to_be_visible()
    d = deployment(api, dep)
    assert d["state"] in ("failed", "pending") and d["error"] == "bad_image"
    assert usb.restarts == 0


def test_an_update_cut_off_is_offered_again(admin_page, context, usb, location, api):
    page = admin_page
    rel = release(version(), size=600_000)  # long enough to close the page part way
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    registered(api, usb.reader)
    (dep,) = deploy(api, upload_release(api, rel), [usb.reader])
    page.reload()
    page.get_by_role("button", name="Update now").click()
    expect(page.get_by_text(re.compile(r"Updating the scanner to"))).to_be_visible()
    page.close()

    page = context.new_page()
    open_panel(page, location)
    expect(page.get_by_text(f"Firmware {rel.version} for this scanner")).to_be_visible()
    assert deployment(api, dep)["state"] not in ("confirmed", "failed")


# Offered without a reload, as a notification on any page (UpdateOverlay.tsx) ------------------


@pytest.fixture
def fast_recheck(context):
    """The service checks the scanner in again every half second, not every 30."""
    context.add_init_script("window.__NFC_SCANNER_POLL_MS = 500;")


def connected(page, location, api, usb):
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    registered(api, usb.reader)


def test_an_update_deployed_after_connecting_is_offered_without_a_reload(
    fast_recheck, admin_page, usb, location, api
):
    page = admin_page
    connected(page, location, api, usb)
    rel = release(version())
    deploy(api, upload_release(api, rel), [usb.reader])
    expect(page.get_by_text(f"Firmware {rel.version} for this scanner")).to_be_visible()


def test_the_offer_reaches_a_page_with_nothing_of_the_plugin_on_it(
    fast_recheck, admin_page, usb, location, api
):
    page = admin_page
    connected(page, location, api, usb)
    # The header's Parts tab (the page has one of its own too): moving within the app, as
    # clicking does, so the service carries on.
    page.get_by_role("tab", name="Parts", exact=True).first.click()
    expect(page).to_have_url(re.compile("/web/part"))
    rel = release(version())
    usb.next_fw = rel.version
    (dep,) = deploy(api, upload_release(api, rel), [usb.reader])
    expect(page.get_by_text(f"Firmware {rel.version} for this scanner")).to_be_visible()
    page.get_by_role("button", name="Update now").click()
    expect(
        page.get_by_text(f"Scanner updated to firmware {rel.version}.")
    ).to_be_visible()
    assert deployment(api, dep)["state"] == "confirmed"
    (
        expect(page).to_have_url(re.compile("/web/part")),
        "and the user stays where they were",
    )


def test_later_holds_until_the_scanner_reconnects(
    fast_recheck, admin_page, usb, location, api
):
    page = admin_page
    connected(page, location, api, usb)
    rel = release(version())
    (dep,) = deploy(api, upload_release(api, rel), [usb.reader])
    offer = page.get_by_text(f"Firmware {rel.version} for this scanner")
    expect(offer).to_be_visible()
    page.get_by_role("button", name="Later").click()
    expect(offer).to_be_hidden()
    page.wait_for_timeout(2000)  # four check-ins
    expect(offer).to_be_hidden()
    assert deployment(api, dep)["deferrals"] == 1
    unplug(page)
    expect(usb_badge(page)).to_have_text("not connected")
    replug(page)
    expect(offer).to_be_visible()


def test_a_deployment_made_required_installs_without_asking(
    fast_recheck, admin_page, usb, location, api
):
    page = admin_page
    connected(page, location, api, usb)
    rel = release(version())
    usb.next_fw = rel.version
    (dep,) = deploy(api, upload_release(api, rel), [usb.reader], required=True)
    expect(
        page.get_by_text(f"Scanner updated to firmware {rel.version}.")
    ).to_be_visible()
    assert deployment(api, dep)["state"] == "confirmed"


def test_checking_in_again_does_not_cut_an_update_short(
    fast_recheck, admin_page, usb, location, api
):
    page = admin_page
    connected(page, location, api, usb)
    rel = release(version(), size=150_000)  # sent over many check-in intervals
    usb.next_fw = rel.version
    (dep,) = deploy(api, upload_release(api, rel), [usb.reader])
    page.get_by_role("button", name="Update now").click()
    # Allowing for a busy machine: the bridge carries the image a piece at a time.
    expect(
        page.get_by_text(f"Scanner updated to firmware {rel.version}.")
    ).to_be_visible(timeout=60_000)
    # Confirmed at the first attempt: no check-in cut it short.
    d = deployment(api, dep)
    assert d["state"] == "confirmed" and d["attempts"] == 1


def test_a_deploy_from_the_fleet_page_reaches_the_scanner_in_another_tab_at_once(
    admin_page, context, usb, location, api
):
    # The usual 30 s between check-ins: what delivers it is the fleet page's word.
    page = admin_page
    connected(page, location, api, usb)
    rel = release(version())
    upload_release(api, rel)

    fleet_tab = context.new_page()
    api.patch(
        "/api/user/me/profile/",
        {
            "widgets": {
                "widgets": ["p-nfcscanner-nfc-fleet"],
                "layouts": {
                    "lg": [
                        {"i": "p-nfcscanner-nfc-fleet", "x": 0, "y": 0, "w": 12, "h": 8}
                    ]
                },
            }
        },
    )
    fleet_tab.goto("/web/home")
    fleet_tab.get_by_label(f"choose {usb.reader}").check()
    fleet_tab.get_by_label("Firmware", exact=True).first.click()
    fleet_tab.get_by_role("option", name=re.compile(re.escape(rel.version))).click()
    fleet_tab.get_by_role("button", name="Deploy to 1 chosen").click()
    expect(fleet_tab.get_by_text("1 deployment made.")).to_be_visible()

    expect(page.get_by_text(f"Firmware {rel.version} for this scanner")).to_be_visible(
        timeout=5000
    )
