"""Firmware updates over USB, as the browser installs them (scanner.ts, UpdateNotice.tsx)."""

import re

from playwright.sync_api import expect

from ..firmware_release import release
from .conftest import deploy, deployment, open_panel, upload_release, usb_badge, version


def offered(page, api, usb, location, **deploy_kw):
    """The scanner seen over USB, then a release deployed to it, then the page again."""
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
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
    (dep,) = deploy(api, upload_release(api, rel), [usb.reader])
    page.reload()
    page.get_by_role("button", name="Update now").click()
    expect(page.get_by_text(re.compile(r"Updating the scanner to"))).to_be_visible()
    page.close()

    page = context.new_page()
    open_panel(page, location)
    expect(page.get_by_text(f"Firmware {rel.version} for this scanner")).to_be_visible()
    assert deployment(api, dep)["state"] not in ("confirmed", "failed")
