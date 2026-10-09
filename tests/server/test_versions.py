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


def test_every_model_is_in_the_admin():
    """InvenTree reloads admin.py when one is missing, and the reload fails on the others."""
    from django.apps import apps
    from django.contrib import admin

    missing = [
        m.__name__
        for m in apps.get_app_config("inventree_nfc_scanner").get_models()
        if not admin.site.is_registered(m)
    ]
    assert missing == []
