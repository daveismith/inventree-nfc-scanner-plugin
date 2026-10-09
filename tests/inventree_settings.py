"""InvenTree's settings, as production has them where the plugin cares.

- The plugin's app is installed from the start. In production the plugin registry adds it when
  it loads the plugin, and `invoke migrate` creates its tables. In a test run the registry
  loads plugins only after the test database exists, too late for its migrations.
- Time zone support is on. InvenTree turns it off under test (to quieten its logs), but in
  production it is on, and the plugin compares the aware times it stores with `timezone.now()`.
"""

from InvenTree.settings import *  # noqa: F403

if "inventree_nfc_scanner" not in INSTALLED_APPS:  # noqa: F405
    INSTALLED_APPS.append("inventree_nfc_scanner")  # noqa: F405

USE_TZ = True
