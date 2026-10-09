"""The fleet dashboard item: firmware on every scanner, releases, deployments. Admins only."""

import json
import re
from pathlib import Path

from playwright.sync_api import expect

from ..firmware_release import release
from .conftest import (
    deploy,
    deployment,
    log_in,
    show_dashboard,
    upload_release,
    version,
)


def fleet_tab(page, name):
    page.get_by_role("tab", name=name).click()


def scanner_row(page, reader):
    return page.get_by_role("row").filter(has_text=reader)


def test_not_shown_to_a_user_who_is_not_an_admin(page, stack, user_with, api):
    user = user_with("stock_location.view", "stock_location.change")
    log_in(page, user["name"], user["password"])
    r = page.request.get("/api/plugins/ui/features/dashboard/")
    keys = {f["key"] for f in r.json() if f["plugin_name"] == "nfcscanner"}
    assert "nfc-fleet" not in keys and "nfc-scanners" in keys


def test_check_now_fetches_from_github(page, stack, api, github):
    rel = release(version())
    github.publish(rel)
    show_dashboard(page, api, "nfc-fleet")
    fleet_tab(page, "Releases")
    page.get_by_role("button", name="Check now").click()
    expect(
        page.get_by_text(re.compile(rf"Fetched .*{re.escape(rel.version)}"))
    ).to_be_visible()
    expect(page.get_by_role("row").filter(has_text=rel.version)).to_be_visible()


def test_upload_a_release_and_one_that_is_tampered_with(page, stack, api, tmp_path):
    rel = release(version())
    for name, data in (
        ("manifest.json", json.dumps(rel.manifest).encode()),
        (rel.app_file, rel.app),
    ):
        (tmp_path / name).write_bytes(data)
    bad = tmp_path / "tampered.bin"
    bad.write_bytes(rel.app[:-1] + bytes([rel.app[-1] ^ 1]))
    show_dashboard(page, api, "nfc-fleet")
    fleet_tab(page, "Releases")

    def choose(label, path):
        # Mantine's FileInput is a button that opens the file chooser.
        with page.expect_file_chooser() as chooser:
            page.get_by_label(label, exact=True).click()
        chooser.value.set_files(path)

    def upload(app: Path):
        choose("manifest.json", tmp_path / "manifest.json")
        choose("App image", app)
        page.get_by_role("button", name="Upload").click()

    upload(bad)
    expect(page.get_by_text(re.compile("does not match"))).to_be_visible()
    upload(tmp_path / rel.app_file)
    expect(page.get_by_text(f"Firmware {rel.version} uploaded.")).to_be_visible()


def test_delete_a_release(page, stack, api):
    rel = release(version())
    upload_release(api, rel)
    show_dashboard(page, api, "nfc-fleet")
    fleet_tab(page, "Releases")
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("row").filter(has_text=rel.version).get_by_role(
        "button", name="delete"
    ).click()
    expect(page.get_by_text(f"Firmware {rel.version} deleted.")).to_be_visible()
    expect(page.get_by_role("row").filter(has_text=rel.version)).to_have_count(0)


def test_deploy_to_a_chosen_scanner_then_withdraw(page, stack, api, net_scanner):
    net = net_scanner()
    net.sync()
    rel = release(version())
    upload_release(api, rel)
    show_dashboard(page, api, "nfc-fleet")
    page.get_by_label(f"choose {net.reader}").check()
    page.get_by_label("Firmware", exact=True).first.click()
    page.get_by_role("option", name=re.compile(re.escape(rel.version))).click()
    page.get_by_role("button", name="Deploy to 1 chosen").click()
    row = scanner_row(page, net.reader)
    expect(row.get_by_text("pending")).to_be_visible()
    row.get_by_role("button", name="withdraw").click()
    expect(row.get_by_text("pending")).to_have_count(0)
    states = [
        d["state"]
        for d in api.get("/plugin/nfcscanner/api/fleet/deployments/")
        if d["reader"] == net.reader
    ]
    assert states == ["cancelled"]


def test_a_downgrade_needs_confirming(page, stack, api, net_scanner):
    net = net_scanner()
    older, newer = release(version()), release(version())  # version() counts up
    net.fw = newer.version
    net.sync()
    upload_release(api, older)
    show_dashboard(page, api, "nfc-fleet")

    page.get_by_label(f"choose {net.reader}").check()
    page.get_by_label("Firmware", exact=True).first.click()
    page.get_by_role("option", name=re.compile(re.escape(older.version))).click()
    page.get_by_role("button", name="Deploy to 1 chosen").click()
    expect(
        page.get_by_text(re.compile(r"0 deployments made\. Not deployed: .*older"))
    ).to_be_visible()
    # The firmware stays chosen (choosing it again would clear it); the scanner may not.
    page.get_by_label("allow a downgrade").check()
    box = page.get_by_label(f"choose {net.reader}")
    if not box.is_checked():
        box.check()
    page.get_by_role("button", name="Deploy to 1 chosen").click()
    expect(scanner_row(page, net.reader).get_by_text("pending")).to_be_visible()


def test_forget_a_scanner(page, stack, api, net_scanner):
    net = net_scanner()
    net.sync()
    show_dashboard(page, api, "nfc-fleet")
    page.once("dialog", lambda d: d.accept())
    scanner_row(page, net.reader).get_by_role("button", name="forget").click()
    expect(scanner_row(page, net.reader)).to_have_count(0)


def test_the_history(page, stack, api, net_scanner):
    net = net_scanner()
    net.sync()
    rel = release(version())
    fw = upload_release(api, rel)
    (dep,) = deploy(api, fw, [net.reader])
    api.post(f"/plugin/nfcscanner/api/fleet/deployments/{dep}/cancel/")
    show_dashboard(page, api, "nfc-fleet")
    fleet_tab(page, "History")
    row = (
        page.get_by_role("row").filter(has_text=rel.version).filter(has_text=net.reader)
    )
    expect(row.get_by_text("cancelled")).to_be_visible()
    assert deployment(api, dep)["state"] == "cancelled"
