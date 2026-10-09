"""Linking a tag's UID to a location as its barcode.

InvenTree refuses to link a barcode that some object already carries. A tag being
re-programmed for another bin, or programmed again for the same one, is exactly that case,
so the link here moves the barcode: whatever held it before lets go first, provided the
person asking may change that object.
"""

from __future__ import annotations

import logging
import re

from django.apps import apps
from django.db import transaction

from InvenTree.helpers import hash_barcode
from InvenTree.models import InvenTreeBarcodeMixin

logger = logging.getLogger("inventree")

# An NTAG21x UID: 7 bytes. Exactly 14 hex digits, so that nothing that looks like a product
# barcode (an EAN is 13 decimal digits) can ever be passed off as one.
UID_RE = re.compile(r"^[0-9A-F]{14}$")


class BadUid(ValueError):
    """Not a tag UID."""


class NotPermitted(PermissionError):
    """The actor may not take the barcode from what holds it."""


def clean_uid(value) -> str:
    """The UID as upper-case hex, or raise BadUid."""
    uid = str(value or "").strip().upper()
    if not UID_RE.match(uid):
        raise BadUid("the tag UID, as 14 hex digits")
    return uid


def holders_of(barcode_hash: str):
    """Every object, of any kind, that currently carries this barcode."""
    for model in apps.get_models():
        if not issubclass(model, InvenTreeBarcodeMixin) or model._meta.abstract:
            continue
        yield from model.objects.filter(barcode_hash=barcode_hash)


def _may_change(actor, obj) -> bool:
    opts = obj._meta
    return bool(actor and actor.has_perm(f"{opts.app_label}.change_{opts.model_name}"))


def link_uid(location, uid, actor) -> str:
    """Make `uid` the location's barcode, on behalf of `actor`. Returns what was done.

    Raises BadUid for a malformed UID, NotPermitted when another holder may not be changed
    by the actor, and whatever InvenTree raises when the link itself fails. All or nothing.
    """
    uid = clean_uid(uid)
    if actor is None or not getattr(actor, "is_active", False):
        raise NotPermitted("no active user to link on behalf of")
    # Checked now, not only when the job was queued: a network job is linked when its `done`
    # arrives, on behalf of whoever queued it, who may have lost the permission since.
    if not actor.has_perm("stock.change_stocklocation"):
        raise NotPermitted(f"{actor} may no longer change stock locations")
    barcode_hash = hash_barcode(uid)
    if location.barcode_hash == barcode_hash:
        return "already linked"

    with transaction.atomic():
        moved_from = []
        for other in holders_of(barcode_hash):
            if other == location:
                continue
            if not _may_change(actor, other):
                # Named by kind only: what it is called is not this user's to see.
                raise NotPermitted(
                    f"the tag is already the barcode of a {other._meta.verbose_name}, which you may not change"
                )
            moved_from.append(f"{other._meta.verbose_name} {other}")
            other.unassign_barcode()

        location.assign_barcode(
            barcode_hash=barcode_hash, barcode_data=uid, raise_error=True
        )

    who = getattr(actor, "username", None) or "the scanner"
    if moved_from:
        logger.info(
            "NFC: %s moved barcode %s to location %s from %s",
            who,
            uid,
            location.pk,
            ", ".join(moved_from),
        )
        return f"moved from {', '.join(moved_from)}"
    logger.info("NFC: %s linked barcode %s to location %s", who, uid, location.pk)
    return "linked"
