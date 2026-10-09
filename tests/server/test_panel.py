"""What the location panel reads: the tag's contents, the scanners, and the panel itself."""

from .conftest import BASE_URL
from .scanner import P


def test_the_tag_for_a_location(api, clerk, location):
    r = api(clerk).get(f"{P}/api/location/{location.pk}/tag/")
    assert r.status_code == 200
    tag = r.json()
    assert tag["text"] == f"INV-SL{location.pk}"
    assert tag["uri"] == f"{BASE_URL}/web/stock/location/{location.pk}"
    assert tag["ndef"].startswith("9101")
    assert "pwd" not in tag


def test_the_tag_carries_the_password_for_those_who_may_program(
    api, clerk, location, set_setting
):
    set_setting("TAG_PASSWORD", "a1b2c3d4")
    tag = api(clerk).get(f"{P}/api/location/{location.pk}/tag/").json()
    assert tag["pwd"] == "A1B2C3D4" and tag["pack"] == "0000"


def test_a_user_without_stock_permission_may_not_read_the_tag(api, nobody, location):
    assert api(nobody).get(f"{P}/api/location/{location.pk}/tag/").status_code == 403


def test_nor_list_the_scanners(api, nobody):
    assert api(nobody).get(f"{P}/api/scanners/").status_code == 403


def test_an_unknown_location_is_404(api, clerk):
    assert api(clerk).get(f"{P}/api/location/999999/tag/").status_code == 404


def test_a_base_url_that_cannot_go_on_a_tag_is_400(api, clerk, location):
    from common.models import InvenTreeSetting

    InvenTreeSetting.set_setting("INVENTREE_BASE_URL", "ftp://example.com", None)
    r = api(clerk).get(f"{P}/api/location/{location.pk}/tag/")
    assert r.status_code == 400 and "base_url" in r.json()


def test_scanners_list(scanner, api, clerk):
    (sc,) = api(clerk).get(f"{P}/api/scanners/").json()
    assert sc["id"] == str(scanner.machine.pk)
    assert sc["driver"] == "nfc-network" and sc["reader"] == scanner.reader
    assert not sc["online"] and sc["status"] == "offline", "not heard from yet"
    assert sc["warning"] is None


def test_scanners_sharing_a_user_are_warned_about(
    scanner, network_scanner, nobody, api, clerk
):
    network_scanner(user=nobody)
    warnings = [s["warning"] for s in api(clerk).get(f"{P}/api/scanners/").json()]
    assert all(w and "shares its user" in w for w in warnings)


def test_an_inactive_scanner_is_not_listed(network_scanner, api, clerk):
    network_scanner(active=False)
    assert api(clerk).get(f"{P}/api/scanners/").json() == []


def test_the_panel_is_offered_on_a_location(api, admin_user, location):
    from common.models import InvenTreeSetting

    InvenTreeSetting.set_setting("ENABLE_PLUGINS_INTERFACE", True, None)
    r = api(admin_user).get(
        f"/api/plugins/ui/features/panel/?target_model=stocklocation&target_id={location.pk}"
    )
    assert r.status_code == 200
    (panel,) = [f for f in r.json() if f["plugin_name"] == "nfcscanner"]
    assert panel["source"].endswith(":RenderNfcPanel")
    assert str(panel["context"]["location"]) == str(location.pk)


def test_the_dashboard_item_is_for_admins(api, admin_user, clerk):
    from common.models import InvenTreeSetting

    InvenTreeSetting.set_setting("ENABLE_PLUGINS_INTERFACE", True, None)
    url = "/api/plugins/ui/features/dashboard/"

    def keys(user):
        items = api(user).get(url).json()
        return {f["key"] for f in items if f["plugin_name"] == "nfcscanner"}

    assert "nfc-fleet" in keys(admin_user)
    assert "nfc-fleet" not in keys(clerk)
