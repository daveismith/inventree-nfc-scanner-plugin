"""The NFC scanners dashboard item."""

import re

from playwright.sync_api import expect

from .conftest import show_dashboard
from .usb_scanner import Tag


def scanner_row(page, name):
    return page.get_by_role("row").filter(has_text=name)


def test_a_scanner_online_with_its_last_tap(page, stack, net_scanner, api):
    net = net_scanner()
    net.sync({
        "evt": "tag",
        "uid": "04AABBCCDDEE02",
        "type": "ntag215",
        "text": "INV-SL77",
    })
    show_dashboard(page, api, "nfc-scanners")
    row = scanner_row(page, net.machine["name"])
    expect(row.get_by_text("online")).to_be_visible()
    expect(row.get_by_text("last tap: INV-SL77")).to_be_visible()


def test_a_scanner_never_heard_from_is_offline(page, stack, net_scanner, api):
    net = net_scanner()
    show_dashboard(page, api, "nfc-scanners")
    # "offline" once the driver has set it up in the worker serving the page; "unknown" in one
    # that has not yet (with several server workers, as production has). Not online, either way.
    expect(
        scanner_row(page, net.machine["name"]).get_by_text(
            re.compile("^(offline|unknown)$")
        )
    ).to_be_visible()


def test_the_scanner_plugged_in_here_is_marked(page, stack, net_scanner, usb, api):
    net = net_scanner()
    net.sync()
    usb.reader = net.reader  # the same scanner, on USB here and on the network
    show_dashboard(page, api, "nfc-scanners")
    expect(page.get_by_text("scanner connected")).to_be_visible()
    expect(
        scanner_row(page, net.machine["name"]).get_by_text("USB here")
    ).to_be_visible()
    usb.present(Tag(text="INV-SL5"))
    expect(page.get_by_text("scanner connected, reading INV-SL5")).to_be_visible()
