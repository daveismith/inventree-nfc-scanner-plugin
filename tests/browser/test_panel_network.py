"""The NFC tag panel with a scanner on the network: the job goes through the server."""

import re

from playwright.sync_api import expect

from .conftest import P, open_panel


def network_program(page):
    # With no USB scanner here, the only "Program tag" is the network route's.
    page.get_by_text("Scanner on the network").locator("xpath=..").get_by_role(
        "button", name="Program tag"
    ).click()


def test_a_job_through_a_network_scanner(admin_page, net_scanner, location, api):
    net = net_scanner()
    net.sync()  # online
    page = admin_page
    open_panel(page, location)
    expect(page.get_by_label("Scanner", exact=True).first).to_have_value(
        net.machine["name"]
    )
    network_program(page)
    expect(page.get_by_text("queued: not yet collected by the scanner")).to_be_visible()

    (cmd,) = net.sync()
    assert cmd["cmd"] == "program" and cmd["ndef"].startswith("9101")
    net.ack(cmd)
    net.sync(
        {"rsp": "program", "ok": True, "id": cmd["id"]},
        {"evt": "waiting", "id": cmd["id"], "timeout_ms": 60000},
    )
    expect(page.get_by_text("present a tag to the scanner")).to_be_visible()
    net.sync({
        "evt": "done",
        "id": cmd["id"],
        "uid": "04AABBCCDDEE01",
        "type": "ntag215",
        "protected": False,
    })
    expect(page.get_by_text("done: 04AABBCCDDEE01")).to_be_visible()
    assert api.get(f"/api/stock/location/{location['pk']}/")["barcode_hash"]


def test_a_busy_scanner_says_so_in_words(admin_page, net_scanner, location, api):
    net = net_scanner()
    net.sync()
    api.post(
        f"{P}/api/jobs/",
        {"location": location["pk"], "scanner": net.machine["pk"]},
        expect_status=201,
    )
    page = admin_page
    open_panel(page, location)
    network_program(page)
    expect(
        page.get_by_text(re.compile(r"That scanner is busy with another job \(#\d+"))
    ).to_be_visible()


def test_a_network_job_is_cancelled_from_the_panel(
    admin_page, net_scanner, location, api
):
    net = net_scanner()
    net.sync()
    page = admin_page
    open_panel(page, location)
    network_program(page)
    expect(page.get_by_text("queued: not yet collected by the scanner")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    # The progress note; the job history lists it too.
    expect(
        page.get_by_role("alert").get_by_text("cancelled", exact=True)
    ).to_be_visible()
    assert net.sync() == []
