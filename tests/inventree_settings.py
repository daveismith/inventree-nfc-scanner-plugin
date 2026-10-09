"""InvenTree's settings, with the plugin's app installed from the start.

In production the plugin registry adds the app when it loads the plugin, and `invoke migrate`
creates its tables. In a test run the registry loads plugins only after the test database
exists, too late for its migrations, so the app is listed here instead.
"""

from InvenTree.settings import *  # noqa: F403

if "inventree_nfc_scanner" not in INSTALLED_APPS:  # noqa: F405
    INSTALLED_APPS.append("inventree_nfc_scanner")  # noqa: F405
