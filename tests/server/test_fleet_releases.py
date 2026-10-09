"""Firmware releases held by the server: uploading, the checks on them, serving and removing."""

import hashlib
import json

import pytest

from .releases import release, upload
from .scanner import P


def test_a_genuine_release_is_taken(fleet):
    r = upload(fleet, release("1.1.0"))
    assert r.status_code == 201
    fw = r.json()
    assert (
        fw["version"] == "1.1.0"
        and fw["incompatible"] is None
        and fw["available"]
        and fw["merged"]
    )


def test_uploading_it_again_is_harmless(fleet):
    rel = release("1.1.0")
    first = upload(fleet, rel).json()
    again = upload(fleet, rel)
    assert again.status_code == 201 and again.json()["id"] == first["id"]


def test_a_different_image_under_a_version_already_held_is_refused(fleet):
    upload(fleet, release("1.1.0"))
    r = upload(fleet, release("1.1.0"))
    assert r.status_code == 400 and "different image" in str(r.json())


def test_an_upload_without_the_app_is_refused(fleet):
    from django.core.files.uploadedfile import SimpleUploadedFile

    r = upload(
        fleet, None, files={"manifest": SimpleUploadedFile("manifest.json", b"{}")}
    )
    assert r.status_code == 400


def test_a_manifest_that_is_not_json_is_refused(fleet):
    from django.core.files.uploadedfile import SimpleUploadedFile

    rel = release("1.1.0")
    r = upload(
        fleet,
        rel,
        files={
            "manifest": SimpleUploadedFile("manifest.json", b"{nope"),
            "app": SimpleUploadedFile(rel.app_file, rel.app),
        },
    )
    assert r.status_code == 400 and "JSON" in str(r.json())


def test_an_image_that_does_not_match_its_manifest_is_refused(fleet):
    rel = release("1.1.0")
    r = upload(fleet, rel, app=rel.app[:-1] + bytes([rel.app[-1] ^ 1]))
    assert r.status_code == 400 and "does not match" in str(r.json())


@pytest.mark.parametrize(
    "change, words",
    [
        ({"target": "esp32c3"}, "esp32c3"),
        ({"version": "not a version"}, "version"),
        ({"name": "something_else"}, "not a inventree_nfc_scanner release"),
    ],
)
def test_a_manifest_that_is_not_this_firmwares_is_refused(fleet, change, words):
    rel = release("1.1.0")
    r = upload(fleet, rel, manifest={**rel.manifest, **change})
    assert r.status_code == 400 and words in str(r.json())


def test_a_release_for_another_protocol_is_held_but_incompatible(fleet):
    fw = upload(fleet, release("1.3.0", proto=2)).json()
    assert fw["incompatible"] and "protocol" in fw["incompatible"]


def test_a_release_needing_a_newer_plugin_is_held_but_incompatible(fleet):
    fw = upload(fleet, release("1.4.0", min_plugin="99.0.0")).json()
    assert fw["incompatible"] and "99.0.0" in fw["incompatible"]


# Who may -------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["clerk", "nobody"])
def test_the_fleet_page_is_for_admins_only(db, api, request, who):
    client = api(request.getfixturevalue(who))
    assert client.get(f"{P}/api/fleet/").status_code == 403
    assert client.post(f"{P}/api/fleet/check/").status_code == 403
    assert upload(client, release("1.1.0")).status_code == 403


def test_the_admin_role_with_change_is_a_fleet_admin(api, fleet_admin):
    assert api(fleet_admin).get(f"{P}/api/fleet/").status_code == 200


# The image endpoint --------------------------------------------------------------------------


@pytest.fixture
def held(fleet):
    rel = release("1.1.0")
    upload(fleet, rel)
    return rel


def body(r):
    return b"".join(r.streaming_content) if r.streaming else r.content


def test_the_image_needs_a_user_or_a_token(api, held):
    r = api().get(f"{P}/firmware/{held.version}/{held.app_file}")
    assert r.status_code in (401, 403)


def test_the_scanners_token_fetches_the_image_intact(api, nobody, held):
    r = api(nobody).get(f"{P}/firmware/{held.version}/{held.app_file}")
    assert r.status_code == 200
    assert hashlib.sha256(body(r)).hexdigest() == held.sha256


@pytest.mark.parametrize("path", ["{v}/manifest.json", "9.9.9/{f}", "{v}/other.bin"])
def test_only_the_releases_own_images_are_served(api, nobody, held, path):
    r = api(nobody).get(f"{P}/firmware/" + path.format(v=held.version, f=held.app_file))
    assert r.status_code == 404


def test_a_path_out_of_the_release_serves_nothing(api, nobody, held):
    # Resolved before routing: it reaches InvenTree's catch-all, which sends the browser home.
    r = api(nobody).get(f"{P}/firmware/{held.version}/../{held.app_file}")
    assert r.status_code != 200 and held.app not in body(r)


# Removing ------------------------------------------------------------------------------------


def test_deleting_an_upload_nothing_refers_to_removes_it(fleet, held):
    (fw,) = fleet.get(f"{P}/api/fleet/").json()["firmware"]
    assert fleet.delete(f"{P}/api/fleet/firmware/{fw['id']}/").status_code == 204
    assert fleet.get(f"{P}/api/fleet/").json()["firmware"] == []


def test_deleting_a_release_with_history_keeps_its_record_without_images(
    fleet, scanner, held, api, nobody
):
    scanner.sync()
    (fw,) = fleet.get(f"{P}/api/fleet/").json()["firmware"]
    dep = fleet.post(
        f"{P}/api/fleet/deploy/",
        {"firmware": fw["id"], "scanners": [scanner.reader]},
        format="json",
    ).json()
    fleet.post(f"{P}/api/fleet/deployments/{dep['results'][0]['deployment']}/cancel/")
    r = fleet.delete(f"{P}/api/fleet/firmware/{fw['id']}/")
    assert r.status_code == 200 and r.json()["available"] is False
    assert (
        api(nobody).get(f"{P}/firmware/{held.version}/{held.app_file}").status_code
        == 404
    )


def test_a_release_being_deployed_cannot_be_deleted(fleet, scanner, held):
    scanner.sync()
    (fw,) = fleet.get(f"{P}/api/fleet/").json()["firmware"]
    fleet.post(
        f"{P}/api/fleet/deploy/",
        {"firmware": fw["id"], "scanners": [scanner.reader]},
        format="json",
    )
    assert fleet.delete(f"{P}/api/fleet/firmware/{fw['id']}/").status_code == 400


def test_pruning_keeps_the_newest_images(fleet):
    from inventree_nfc_scanner import firmware as fwlib
    from inventree_nfc_scanner.models import Firmware

    for v in ("1.1.0", "1.2.0", "1.3.0"):
        upload(fleet, release(v))
    assert sorted(fwlib.prune(2)) == ["1.1.0"]
    assert [
        f.version for f in Firmware.objects.exclude(app="").order_by("version")
    ] == ["1.2.0", "1.3.0"]
    assert fwlib.prune(0) == []


def test_the_fleet_overview(fleet, scanner, held):
    scanner.sync()
    overview = fleet.get(f"{P}/api/fleet/").json()
    (sc,) = overview["scanners"]
    assert (
        sc["reader"] == scanner.reader
        and sc["fw"] == "1.0.0"
        and sc["last_via"] == "network"
    )
    assert sc["machine"]["name"] == scanner.machine.name
    assert sc["outdated"] is True
    assert overview["newest"] == "1.1.0"
    assert overview["policy"] == "deferrable"
    assert json.dumps(overview)  # all of it serialisable


def test_forgetting_a_scanner(fleet, scanner):
    scanner.sync()
    assert fleet.delete(f"{P}/api/fleet/scanners/{scanner.reader}/").status_code == 204
    assert fleet.get(f"{P}/api/fleet/").json()["scanners"] == []
    scanner.sync()
    assert len(fleet.get(f"{P}/api/fleet/").json()["scanners"]) == 1, (
        "it comes back when heard from"
    )
