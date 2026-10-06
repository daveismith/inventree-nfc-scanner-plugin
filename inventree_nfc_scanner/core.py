"""InvenTree NFC Scanner: program and read the NFC tags on storage bins.

Two routes to a tag:

- USB: the browser drives a scanner plugged into the user's computer over WebSerial. The
  server supplies the tag's data and records the outcome.
- Network: a scanner on Wi-Fi polls this plugin for jobs (`/sync`) and reports back. Each
  such scanner is an InvenTree machine.

See docs/api.md for the endpoints, and the firmware repository for the scanner.
"""

from django.utils.translation import gettext_lazy as _

from plugin import InvenTreePlugin
from plugin.mixins import (
    AppMixin,
    MachineDriverMixin,
    ScheduleMixin,
    SettingsMixin,
    UrlsMixin,
    UserInterfaceMixin,
)

from . import PLUGIN_VERSION


class InvenTreeNFCScanner(
    AppMixin, SettingsMixin, UrlsMixin, UserInterfaceMixin, MachineDriverMixin, ScheduleMixin, InvenTreePlugin
):
    """The plugin."""

    TITLE = 'InvenTree NFC Scanner'
    NAME = 'InvenTreeNFCScanner'
    SLUG = 'nfcscanner'
    DESCRIPTION = 'Program and read the NFC tags on storage bins, over USB (WebSerial) or from a networked scanner'
    VERSION = PLUGIN_VERSION

    AUTHOR = 'David Smith'
    WEBSITE = 'https://github.com/daveismith/inventree-nfc-scanner-plugin'
    LICENSE = 'MIT'

    MIN_VERSION = '1.0.0'

    SETTINGS = {
        'TAG_PASSWORD': {
            'name': _('Tag password'),
            'description': _('Eight hex digits. Tags are write-protected with it after programming. Blank: no protection.'),
            'default': '',
            'protected': True,
        },
        'TAG_PACK': {
            'name': _('Tag password acknowledge (PACK)'),
            'description': _('Four hex digits the tag answers a correct password with'),
            'default': '0000',
        },
        'JOB_TIMEOUT_S': {
            'name': _('Job timeout (seconds)'),
            'description': _('How long a scanner waits for a tag to be presented'),
            'validator': int,
            'default': 60,
        },
        'LONG_POLL': {
            'name': _('Long polling'),
            'description': _(
                'Hold a scanner\'s request until a job is queued for it. Delivers jobs at once, but occupies a '
                'server worker per scanner; off, scanners poll once a second.'
            ),
            'validator': bool,
            'default': False,
        },
        'LONG_POLL_MAX_S': {
            'name': _('Longest hold (seconds)'),
            'description': _('Keep under the proxy\'s request timeout'),
            'validator': int,
            'default': 25,
        },
        'SCANNER_OFFLINE_S': {
            'name': _('Offline after (seconds)'),
            'description': _('A scanner not heard from for this long is shown offline'),
            'validator': int,
            'default': 40,
        },
    }

    SCHEDULED_TASKS = {
        'check_scanners': {'func': 'check_scanners', 'schedule': 'I', 'minutes': 1},
    }

    def check_scanners(self):
        """Mark scanners that have gone quiet as offline (runs every minute)."""
        from .sync import mark_stale_scanners

        mark_stale_scanners(int(self.get_setting('SCANNER_OFFLINE_S') or 40))

    # Machines

    def get_machine_types(self):
        """The NFC scanner machine type."""
        from .machine import NfcScannerMachine

        return [NfcScannerMachine]

    def get_machine_drivers(self):
        """The network driver."""
        from .machine import NetworkScannerDriver

        return [NetworkScannerDriver]

    # URLs: everything is under /plugin/nfcscanner/

    def setup_urls(self):
        """The endpoints; see docs/api.md."""
        from django.urls import path

        from . import views

        return [
            path('sync/', views.SyncView.as_view(), name='sync'),
            path('api/location/<int:pk>/tag/', views.LocationTagView.as_view(), name='location-tag'),
            path('api/location/<int:pk>/jobs/usb/', views.UsbJobView.as_view(), name='location-usb-job'),
            path('api/location/<int:pk>/link/', views.LinkView.as_view(), name='location-link'),
            path('api/scanners/', views.ScannerListView.as_view(), name='scanners'),
            path('api/jobs/', views.JobListView.as_view(), name='jobs'),
            path('api/jobs/<int:pk>/', views.JobDetailView.as_view(), name='job'),
            path('api/jobs/<int:pk>/cancel/', views.JobCancelView.as_view(), name='job-cancel'),
        ]

    # User interface

    def panel_context(self, request, location_id):
        """What the panel needs to know up front."""
        return {
            'location': location_id,
            'api': '/plugin/nfcscanner/api/',
            'has_password': bool((self.get_setting('TAG_PASSWORD') or '').strip()),
            'job_timeout_s': int(self.get_setting('JOB_TIMEOUT_S') or 60),
            'can_program': bool(request.user and request.user.has_perm('stock.change_stocklocation')),
        }

    def get_ui_panels(self, request, context: dict, **kwargs):
        """The "NFC tag" panel on a stock location's page."""
        context = context or {}
        if context.get('target_model') != 'stocklocation' or not context.get('target_id'):
            return []
        return [{
            'key': 'nfc-tag',
            'title': 'NFC tag',
            'description': 'Program the NFC tag on this bin',
            'icon': 'ti:nfc:outline',
            'source': self.plugin_static_file('Panel.js:RenderNfcPanel'),
            'context': self.panel_context(request, context.get('target_id')),
        }]

    def get_ui_dashboard_items(self, request, context: dict, **kwargs):
        """The scanners and what they last saw."""
        return [{
            'key': 'nfc-scanners',
            'title': 'NFC scanners',
            'description': 'Network NFC scanners and their last tap',
            'icon': 'ti:nfc:outline',
            'source': self.plugin_static_file('Dashboard.js:RenderNfcDashboardItem'),
            'options': {'width': 3, 'height': 2},
            'context': {'api': '/plugin/nfcscanner/api/'},
        }]
