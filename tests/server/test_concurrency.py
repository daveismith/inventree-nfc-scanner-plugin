"""What the row locks are for: two requests at once. PostgreSQL only (SQLite locks the whole
database, so these cannot go wrong there); run weekly, or with tests/run.sh --db postgres."""

import threading

import pytest

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
]

THREADS = 8


def at_once(fn, n=THREADS, tolerate=()):
    """Run fn(i) in n threads released together; re-raise the first failure not tolerated.
    Returns the tolerated ones."""
    from django.db import connection

    barrier = threading.Barrier(n)
    errors, refused = [], []

    def run(i):
        try:
            barrier.wait()
            fn(i)
        except tolerate as exc:
            refused.append(exc)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    return refused


def test_commands_queued_at_once_get_distinct_numbers(network_scanner):
    from inventree_nfc_scanner.models import ScannerCommand
    from inventree_nfc_scanner.sync import enqueue

    config = network_scanner().machine_config
    at_once(lambda i: enqueue(config, {"cmd": "info", "id": i}))
    seqs = sorted(
        ScannerCommand.objects.filter(machine=config).values_list("seq", flat=True)
    )
    assert seqs == list(range(1, THREADS + 1))


def test_one_uid_linked_to_several_bins_at_once_ends_on_one(clerk):
    from inventree_nfc_scanner import barcodes

    from .conftest import make_location

    from django.core.exceptions import ValidationError

    bins = [make_location() for _ in range(THREADS)]
    # A link that loses the race may be refused (InvenTree's "Existing barcode found"); what
    # must not happen is the tag ending up as the barcode of two bins.
    at_once(
        lambda i: barcodes.link_uid(bins[i], "04A1B2C3D4E5F6", clerk),
        tolerate=(ValidationError,),
    )
    for b in bins:
        b.refresh_from_db()
    holders = [b.pk for b in bins if b.barcode_hash]
    assert len(holders) == 1, f"the tag is the barcode of {len(holders)} bins"


def test_a_deployment_is_handed_out_once_to_simultaneous_syncs(scanner, fleet):
    from .releases import release, upload
    from .scanner import P

    scanner.sync()
    fw = upload(fleet, release("1.1.0")).json()
    fleet.post(
        f"{P}/api/fleet/deploy/",
        {"firmware": fw["id"], "scanners": [scanner.reader]},
        format="json",
    )
    got = []
    at_once(
        lambda i: got.extend(c for c in scanner.cmds() if c.get("cmd") == "ota"), n=4
    )
    assert len({c["seq"] for c in got}) == 1
