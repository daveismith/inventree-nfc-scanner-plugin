"""Linking a tag's UID to a location as its barcode.

InvenTree refuses to link a barcode that some object already carries. A tag being
re-programmed for another bin, or programmed again for the same one, is exactly that case,
so the link here moves the barcode: whatever held it before lets go first.
"""

from django.apps import apps

from InvenTree.helpers import hash_barcode
from InvenTree.models import InvenTreeBarcodeMixin


def holders_of(barcode_hash: str):
    """Every object, of any kind, that currently carries this barcode."""
    for model in apps.get_models():
        if not issubclass(model, InvenTreeBarcodeMixin) or model._meta.abstract:
            continue
        for obj in model.objects.filter(barcode_hash=barcode_hash):
            yield obj


def link_uid(location, uid: str) -> str:
    """Make `uid` the location's barcode. Returns what was done, for the record."""
    barcode_hash = hash_barcode(uid)
    if location.barcode_hash == barcode_hash:
        return 'already linked'

    moved_from = []
    for other in holders_of(barcode_hash):
        if other == location:
            continue
        moved_from.append(f'{other._meta.verbose_name} {other}')
        other.unassign_barcode()

    location.assign_barcode(barcode_hash=barcode_hash, barcode_data=uid, raise_error=True)
    return f'moved from {", ".join(moved_from)}' if moved_from else 'linked'
