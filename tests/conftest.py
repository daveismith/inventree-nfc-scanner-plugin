"""Layer markers from the directory a test lives in: tests/pure, tests/server, ..."""

from pathlib import Path

import pytest

LAYERS = ("pure", "server", "browser", "firmware")
HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    for item in items:
        layer = Path(item.path).relative_to(HERE).parts[0]
        if layer in LAYERS:
            item.add_marker(getattr(pytest.mark, layer))
