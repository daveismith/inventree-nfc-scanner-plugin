"""The path everything else builds on: log in, open a bin, connect the scanner, program a tag."""

import re

from playwright.sync_api import expect

from .conftest import jobs_of
from .usb_scanner import Tag


def test_program_a_blank_tag_over_usb(admin_page, usb, location, api):
    page = admin_page
    page.goto(f"/web/stock/location/{location['pk']}/nfc-tag")
    panel = page.get_by_text("Scanner on this computer").locator(
        "xpath=ancestor::div[2]"
    )
    expect(panel.get_by_text("connected")).to_be_visible()

    page.get_by_role("button", name="Program tag").first.click()
    expect(page.get_by_text("present the tag to the scanner")).to_be_visible()
    usb.present(Tag())
    expect(page.get_by_text("Programmed", exact=True)).to_be_visible()

    (program,) = usb.commands("program")
    assert program["ndef"].startswith("9101")
    assert usb.tag.text == f"INV-SL{location['pk']}"
    jobs = jobs_of(api, location)
    assert jobs[0]["state"] == "done" and jobs[0]["uid"] == "04A1B2C3D4E5F6"
    assert re.search(r"/web/stock/location/\d+$", usb.tag.uri)
