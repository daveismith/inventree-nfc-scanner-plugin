"""The plugin agrees with what it is tested against, and its migrations with its models."""

import json
import os
import re
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

VERSIONS = json.loads(
    (Path(__file__).parents[1] / "inventree-versions.json").read_text()
)["supported"]


def test_min_version_is_the_oldest_version_tested():
    from inventree_nfc_scanner.core import InvenTreeNFCScanner

    assert InvenTreeNFCScanner.MIN_VERSION == VERSIONS[0]


def test_this_run_is_against_a_supported_version():
    """Unless it is the nightly run against InvenTree's next release (the `latest` image)."""
    from InvenTree.version import inventreeVersion

    asked = os.environ.get("INVENTREE_TEST_VERSION", "")
    if asked and not re.fullmatch(r"\d+\.\d+\.\d+", asked):
        pytest.skip(f"a run against InvenTree {asked}")
    assert inventreeVersion() in VERSIONS


def test_the_migrations_match_the_models(db):
    out = StringIO()
    # Exits non-zero (SystemExit) if a migration is missing.
    call_command(
        "makemigrations",
        "inventree_nfc_scanner",
        "--check",
        "--dry-run",
        stdout=out,
        stderr=out,
    )
