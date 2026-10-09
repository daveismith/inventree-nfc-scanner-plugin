"""A tag's UID as its bin's barcode: linking, moving, and who may."""

import pytest

from .conftest import make_location
from .scanner import P

UID = "04A1B2C3D4E5F6"


@pytest.fixture
def link(api, clerk):
    client = api(clerk)
    return lambda loc, body: client.post(
        f"{P}/api/location/{loc.pk}/link/", body, format="json"
    )


def test_a_uid_is_linked(link, location):
    r = link(location, {"uid": UID})
    assert r.status_code == 200 and r.json()["outcome"] == "linked"
    location.refresh_from_db()
    assert location.barcode_hash


def test_lower_case_and_spaces_are_cleaned(link, location):
    r = link(location, {"uid": " 04a1b2c3d4e5f6 "})
    assert r.status_code == 200 and r.json()["uid"] == UID


def test_linking_again_is_a_no_op(link, location):
    link(location, {"uid": UID})
    assert link(location, {"uid": UID}).json()["outcome"] == "already linked"


def test_the_same_uid_on_another_bin_is_moved_there(link, location):
    other = make_location()
    link(location, {"uid": UID})
    r = link(other, {"uid": UID})
    assert r.status_code == 200 and r.json()["outcome"].startswith("moved from")
    location.refresh_from_db()
    assert not location.barcode_hash, "the first bin no longer carries it"
    assert link(location, {"uid": UID}).json()["outcome"].startswith("moved from"), (
        "and back again"
    )


@pytest.mark.parametrize("body", [{"uid": "not hex"}, {"uid": "4006381333931"}, [], {}])
def test_what_is_not_a_uid_is_400(link, location, body):
    assert link(location, body).status_code == 400


def test_linking_needs_change_permission(api, nobody, location):
    r = api(nobody).post(
        f"{P}/api/location/{location.pk}/link/", {"uid": UID}, format="json"
    )
    assert r.status_code == 403


def test_a_user_who_lost_the_permission_cannot_link_on_their_own_behalf(
    location, nobody
):
    from inventree_nfc_scanner import barcodes

    with pytest.raises(barcodes.NotPermitted):
        barcodes.link_uid(location, UID, nobody)


def test_an_inactive_user_cannot_link(location, clerk):
    from inventree_nfc_scanner import barcodes

    clerk.is_active = False
    clerk.save()
    with pytest.raises(barcodes.NotPermitted):
        barcodes.link_uid(location, UID, clerk)
