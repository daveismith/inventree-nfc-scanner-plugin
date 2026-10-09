"""Layer markers from the directory a test lives in: tests/pure, tests/server, ...

Tests marked `postgres` are skipped unless the run is against PostgreSQL (tests/run.sh --db).
"""

import os
from pathlib import Path

import pytest

LAYERS = ("pure", "server", "browser", "firmware")
HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    for item in items:
        layer = Path(item.path).relative_to(HERE).parts[0]
        if layer in LAYERS:
            item.add_marker(getattr(pytest.mark, layer))
        if (
            "postgres" in item.keywords
            and os.environ.get("INVENTREE_TEST_DB") != "postgres"
        ):
            item.add_marker(
                pytest.mark.skip(reason="needs PostgreSQL (tests/run.sh --db postgres)")
            )
