"""Loaded with `-p tests.inventree_setup` (pyproject.toml), before pytest-django starts Django.

- InvenTree decides it is under test from its command line (InvenTree/ready.py). "test" there,
  as `manage.py test` has, keeps its start-up from checking migrations on the configured
  database, and exiting since nothing migrated it, before pytest-django creates the test one.
- The settings are InvenTree's with the plugin's app added (tests/inventree_settings.py), so the
  test database is migrated with the plugin's tables, as `invoke migrate` does in production.

Without InvenTree (the pure layer), neither matters.
"""

import os
import sys

if "test" not in sys.argv:
    sys.argv.append("test")

if os.environ.get("DJANGO_SETTINGS_MODULE") == "InvenTree.settings":
    os.environ["DJANGO_SETTINGS_MODULE"] = "tests.inventree_settings"
