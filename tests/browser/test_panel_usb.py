"""The NFC tag panel on a bin's page, with the scanner on USB."""

import re

import pytest

from playwright.sync_api import expect

from .conftest import P, jobs_of, make_location, open_panel, unplug, usb_badge
from .usb_scanner import Tag


def program(page):
    page.get_by_role("button", name="Program tag").first.click()


def test_connect_asks_for_the_scanner_once(admin_page, usb_new, location):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("not connected")
    page.get_by_role("button", name="Connect scanner").click()
    expect(usb_badge(page)).to_have_text("connected")
    assert usb_new.commands("hid") == [{"cmd": "hid", "enabled": False}]
    # Granted now: the next visit connects by itself.
    page.reload()
    expect(usb_badge(page)).to_have_text("connected")


def test_a_granted_scanner_connects_by_itself(admin_page, usb, location):
    open_panel(admin_page, location)
    expect(usb_badge(admin_page)).to_have_text("connected")
    assert usb.commands("info")


def test_a_tag_that_is_not_blank_is_shown_and_can_be_overwritten(
    admin_page, usb, location, api
):
    page = admin_page
    open_panel(page, location)
    usb.tag = Tag(text="INV-SL9999", uri="https://elsewhere/web/stock/location/9999")
    program(page)
    expect(page.get_by_text("Not programmed")).to_be_visible()
    expect(
        page.get_by_text("The tag already holds a message. Replace it?")
    ).to_be_visible()
    page.get_by_role("button", name="Overwrite").click()
    expect(page.get_by_text("Programmed", exact=True)).to_be_visible()
    assert usb.commands("program")[-1]["overwrite"] is True
    assert usb.tag.text == f"INV-SL{location['pk']}"
    states = [j["state"] for j in jobs_of(api, location, count=2)]
    assert states == ["done", "failed"]


def test_tags_are_protected_with_the_tag_password(
    admin_page, usb, location, plugin_setting
):
    plugin_setting("TAG_PASSWORD", "A1B2C3D4")
    page = admin_page
    open_panel(page, location)
    program(page)
    usb.present(Tag())
    expect(
        page.get_by_text(re.compile(r"done: 04A1B2C3D4E5F6, protected"))
    ).to_be_visible()
    assert usb.commands("program")[0]["pwd"] == "A1B2C3D4"


def test_a_tag_protected_with_another_password_is_refused(
    admin_page, usb, location, plugin_setting
):
    plugin_setting("TAG_PASSWORD", "A1B2C3D4")
    page = admin_page
    open_panel(page, location)
    program(page)
    usb.present(Tag(protected=True, pwd="0BADF00D"))
    expect(page.get_by_text("failed: auth_failed")).to_be_visible()


def test_cancel_while_waiting(admin_page, usb, location, api):
    page = admin_page
    open_panel(page, location)
    program(page)
    expect(page.get_by_text("present the tag to the scanner")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect(page.get_by_text("failed: cancelled")).to_be_visible()
    assert jobs_of(api, location)[0]["state"] == "cancelled"


def test_unplugged_mid_job(admin_page, usb, location, api):
    page = admin_page
    open_panel(page, location)
    program(page)
    expect(page.get_by_text("present the tag to the scanner")).to_be_visible()
    unplug(page)
    expect(page.get_by_text("the scanner disconnected during the job")).to_be_visible()
    expect(usb_badge(page)).to_have_text("not connected")
    job = jobs_of(api, location)[0]
    assert job["state"] == "failed" and job["error"] == "no_scanner"


def test_a_refused_program_command(admin_page, usb, location):
    usb.refuse["program"] = "nfc_error"
    open_panel(admin_page, location)
    program(admin_page)
    expect(admin_page.get_by_text("refused: nfc_error")).to_be_visible()


def test_a_tag_moved_from_another_bin(admin_page, usb, location, api):
    other = make_location(api, "Old bin")
    api.post(f"{P}/api/location/{other['pk']}/link/", {"uid": "04A1B2C3D4E5F6"})
    page = admin_page
    open_panel(page, location)
    program(page)
    usb.present(Tag(text=f"INV-SL{other['pk']}"))
    expect(
        page.get_by_text("The tag already holds a message. Replace it?")
    ).to_be_visible()
    page.get_by_role("button", name="Overwrite").click()
    expect(page.get_by_text(re.compile(r"UID moved from"))).to_be_visible()
    assert not api.get(f"/api/stock/location/{other['pk']}/")["barcode_hash"]
    assert api.get(f"/api/stock/location/{location['pk']}/")["barcode_hash"]


def test_a_tap_on_another_bins_tag_opens_that_bin(admin_page, usb, location, api):
    other = make_location(api, "Tapped bin")
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    usb.present(
        Tag(
            text=f"INV-SL{other['pk']}",
            uri=f"http://localhost:8000/web/stock/location/{other['pk']}",
        )
    )
    expect(page).to_have_url(re.compile(rf"/web/stock/location/{other['pk']}"))


def test_without_permission_the_panel_says_so(page, stack, usb, location, user_with):
    from .conftest import log_in

    user = user_with("stock_location.view")
    log_in(page, user["name"], user["password"])
    page.goto(f"/web/stock/location/{location['pk']}/nfc-tag")
    expect(
        page.get_by_text(
            "You need permission to change stock locations to program tags."
        )
    ).to_be_visible()


@pytest.mark.xfail(
    strict=True, reason="docs/open-issues.md 6: the panel's route is not matched"
)
def test_a_tap_on_this_bins_own_tag_stays_on_its_panel(admin_page, usb, location):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    here = f"http://localhost:8000/web/stock/location/{location['pk']}"
    usb.present(Tag(text=f"INV-SL{location['pk']}", uri=here))
    page.wait_for_timeout(1500)  # a navigation would have happened by now
    expect(page).to_have_url(re.compile(r"/nfc-tag$"), timeout=1000)
