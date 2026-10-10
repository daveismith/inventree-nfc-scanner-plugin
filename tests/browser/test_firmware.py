"""The browser against the firmware's own code: the panel and USB updates with the firmware's
simulator (host_sim) behind WebSerial, and a job through its real /sync client.

Marked `firmware`: tests/browser/run.sh --firmware runs them, with the simulator from
tests/browser/sim.sh. They check what usb_scanner.py stands in for, so where it and the
firmware disagree, these are the ones that are right.
"""

import re
import secrets

import pytest
from playwright.sync_api import expect

from ..firmware_release import release
from .conftest import (
    P,
    deploy,
    deployment,
    jobs_of,
    open_panel,
    registered,
    upload_release,
    usb_badge,
    version,
)

pytestmark = pytest.mark.firmware


def program(page):
    page.get_by_role("button", name="Program tag").first.click()


def test_connects_and_reports_itself(admin_page, sim_usb, location):
    open_panel(admin_page, location)
    expect(usb_badge(admin_page)).to_have_text("connected")
    (info,) = [m for m in sim_usb.lines("<") if m.get("rsp") == "info"]
    assert info["reader"] == sim_usb.reader and info["fw"]
    assert sim_usb.commands("hid") == [{"cmd": "hid", "enabled": False}]


def test_programs_a_blank_tag(admin_page, sim_usb, location, api):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    program(page)
    expect(page.get_by_text("present the tag to the scanner")).to_be_visible()
    sim_usb.present("ntag215")
    expect(page.get_by_text("Programmed", exact=True)).to_be_visible()
    (done,) = sim_usb.events("done")
    job = jobs_of(api, location)[0]
    assert job["state"] == "done" and job["uid"] == done["uid"]
    assert api.get(f"/api/stock/location/{location['pk']}/")["barcode_hash"]


def test_a_programmed_tag_reads_back_as_this_bin(admin_page, sim_usb, location):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    program(page)
    sim_usb.present("ntag215")
    expect(page.get_by_text("Programmed", exact=True)).to_be_visible()
    sim_usb.tap()  # off the reader and back: the firmware reads it again
    # What the firmware read off the tag. (The page then leaves the panel for the bin's own
    # page: docs/open-issues.md 6, as the browser tests' xfail has it.)
    expect(page).to_have_url(
        re.compile(rf"/web/stock/location/{location['pk']}(/details)?$")
    )
    tap = sim_usb.events("tag")[-1]
    assert tap["text"] == f"INV-SL{location['pk']}"
    assert tap["uri"].endswith(f"/web/stock/location/{location['pk']}")


def test_a_tag_that_is_not_blank_can_be_overwritten(admin_page, sim_usb, location, api):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    program(page)
    sim_usb.present("ntag215")
    expect(page.get_by_text("Programmed", exact=True)).to_be_visible()

    program(page)  # the same tag, still on the reader: it holds a message now
    expect(
        page.get_by_text("The tag already holds a message. Replace it?")
    ).to_be_visible()
    page.get_by_role("button", name="Overwrite").click()
    expect(page.get_by_text("Programmed", exact=True)).to_be_visible()
    states = [j["state"] for j in jobs_of(api, location, count=3)]
    assert states == ["done", "failed", "done"]


def test_a_tag_is_protected_with_the_tag_password(
    admin_page, sim_usb, location, plugin_setting
):
    plugin_setting("TAG_PASSWORD", "A1B2C3D4")
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    program(page)
    sim_usb.present("ntag215")
    expect(
        page.get_by_text(re.compile(r"done: [0-9A-F]{14}, protected"))
    ).to_be_visible()


def test_a_tag_pulled_away_mid_write_fails_the_job(admin_page, sim_usb, location, api):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    # A tag on the reader, set to leave the field after two more page writes (a new tag resets
    # that, so it comes first), then the job.
    sim_usb.present("ntag215")
    sim_usb.tear_after(2)
    program(page)
    expect(
        page.get_by_text(re.compile(r"failed: (tag_removed|write_failed)"))
    ).to_be_visible()
    assert jobs_of(api, location)[0]["state"] == "failed"


def test_a_tag_of_another_kind_is_refused(admin_page, sim_usb, location):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    program(page)
    sim_usb.present("classic")
    expect(page.get_by_text("failed: wrong_tag_type")).to_be_visible()


def test_cancel_while_waiting(admin_page, sim_usb, location):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    program(page)
    expect(page.get_by_text("present the tag to the scanner")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect(page.get_by_text("failed: cancelled")).to_be_visible()


def test_an_update_over_usb(admin_page, sim_usb, location, api):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    registered(api, sim_usb.reader)
    new = version()
    # An image the simulator takes: it restarts reporting the version the image names.
    rel = release(new, app=f"fw={new}\n".encode() + secrets.token_bytes(20_000))
    (dep,) = deploy(api, upload_release(api, rel), [sim_usb.reader])
    page.reload()
    page.get_by_role("button", name="Update now").click()
    expect(page.get_by_text(f"Scanner updated to firmware {new}.")).to_be_visible()
    assert deployment(api, dep)["state"] == "confirmed"
    assert sim_usb.restarts == 1


def test_an_image_whose_digest_does_not_match_is_refused(
    admin_page, sim_usb, location, api
):
    page = admin_page
    open_panel(page, location)
    expect(usb_badge(page)).to_have_text("connected")
    registered(api, sim_usb.reader)
    rel = release(version())
    # The server's record says one digest; the simulator is sent bytes with another. The
    # page checks first where it can (crypto.subtle); without it, as outside a secure context,
    # the firmware's check is the one that refuses.
    page.add_init_script(
        "Object.defineProperty(crypto, 'subtle', { get: () => undefined });"
    )
    fw = upload_release(api, rel)
    (dep,) = deploy(api, fw, [sim_usb.reader])
    page.route(
        re.compile(r"/firmware/.*\.bin$"),
        lambda route: route.fulfill(body=b"\x00" * len(rel.app)),
    )
    page.reload()
    page.get_by_role("button", name="Update now").click()
    expect(
        page.get_by_text(re.compile("The firmware update did not finish"))
    ).to_be_visible()
    assert sim_usb.restarts == 0
    assert deployment(api, dep)["state"] in ("failed", "pending")


def test_a_job_through_the_firmwares_network_link(admin_page, sim_net, location, api):
    import time

    sim = sim_net()
    # Online once the simulator has called in (it calls every 300 ms).
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        mine = [
            s for s in api.get(f"{P}/api/scanners/") if s["id"] == sim.machine["pk"]
        ]
        if mine and mine[0]["online"]:
            break
        time.sleep(0.5)
    else:
        pytest.fail("the simulator never came online")
    page = admin_page
    open_panel(page, location)
    # The panel lists the scanner online once the simulator has called in.
    expect(page.get_by_label("Scanner", exact=True).first).to_have_value(
        sim.machine["name"]
    )
    page.get_by_text("Scanner on the network").locator("xpath=..").get_by_role(
        "button", name="Program tag"
    ).click()
    expect(page.get_by_text("present a tag to the scanner")).to_be_visible()
    sim.present("ntag215")
    expect(page.get_by_text(re.compile(r"done: [0-9A-F]{14}"))).to_be_visible()
    assert api.get(f"/api/stock/location/{location['pk']}/")["barcode_hash"]
    assert api.get(f"{P}/api/jobs/?location={location['pk']}")[0]["state"] == "done"
