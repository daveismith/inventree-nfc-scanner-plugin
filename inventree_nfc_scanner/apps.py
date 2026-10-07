"""Django config for the InvenTreeNFCScanner plugin."""

from django.apps import AppConfig


class InvenTreeNFCScannerConfig(AppConfig):
    """Config class for the InvenTreeNFCScanner plugin."""

    name = "inventree_nfc_scanner"

    def ready(self):
        """This function is called whenever the InvenTreeNFCScanner plugin is loaded."""
        ...
