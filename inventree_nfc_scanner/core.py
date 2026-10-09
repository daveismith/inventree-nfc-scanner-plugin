"""InvenTree NFC Scanner: program and read the NFC tags on storage bins.

Two routes to a tag:

- USB: the browser drives a scanner plugged into the user's computer over WebSerial. The
  server supplies the tag's data and records the outcome.
- Network: a scanner on Wi-Fi polls this plugin for jobs (`/sync`) and reports back. Each
  such scanner is an InvenTree machine.

See docs/api.md for the endpoints, and the firmware repository for the scanner.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
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


def hex_digits(n):
    """A validator for a setting of exactly `n` hex digits, or blank."""

    def validate(value):
        v = str(value or "").strip()
        if v and (len(v) != n or any(c not in "0123456789abcdefABCDEF" for c in v)):
            raise ValidationError(f"{n} hex digits, or blank")

    return validate


class InvenTreeNFCScanner(
    AppMixin,
    SettingsMixin,
    UrlsMixin,
    UserInterfaceMixin,
    MachineDriverMixin,
    ScheduleMixin,
    InvenTreePlugin,
):
    """The plugin."""

    TITLE = "InvenTree NFC Scanner"
    NAME = "InvenTreeNFCScanner"
    SLUG = "nfcscanner"
    DESCRIPTION = "Program and read the NFC tags on storage bins, over USB (WebSerial) or from a networked scanner"
    VERSION = PLUGIN_VERSION

    AUTHOR = "David Smith"
    WEBSITE = "https://github.com/daveismith/inventree-nfc-scanner-plugin"
    LICENSE = "MIT"

    MIN_VERSION = "1.4.3"

    SETTINGS = {
        "TAG_PASSWORD": {
            "name": _("Tag password"),
            "description": _(
                "Eight hex digits. Tags are write-protected with it after programming. Blank: no protection. "
                "Anyone who may program tags receives it, since their scanner needs it."
            ),
            "default": "",
            "protected": True,
            "validator": hex_digits(8),
        },
        "TAG_PACK": {
            "name": _("Tag password acknowledge (PACK)"),
            "description": _("Four hex digits the tag answers a correct password with"),
            "default": "0000",
            "protected": True,
            "validator": hex_digits(4),
        },
        "JOB_TIMEOUT_S": {
            "name": _("Job timeout (seconds)"),
            "description": _(
                "How long a scanner waits for a tag to be presented (1 to 600)"
            ),
            "validator": [int, MinValueValidator(1), MaxValueValidator(600)],
            "default": 60,
        },
        "LONG_POLL": {
            "name": _("Long polling"),
            "description": _(
                "Hold a scanner's request until a job is queued for it. Delivers jobs at once, but occupies a "
                "server worker per scanner; off, scanners poll once a second."
            ),
            "validator": bool,
            "default": False,
        },
        "LONG_POLL_MAX_S": {
            "name": _("Longest hold (seconds)"),
            "description": _(
                "Keep under the proxy's request timeout; at most 60. Each held call occupies a server worker."
            ),
            "validator": [int, MinValueValidator(0), MaxValueValidator(60)],
            "default": 25,
        },
        "SCANNER_OFFLINE_S": {
            "name": _("Offline after (seconds)"),
            "description": _(
                "A scanner not heard from for this long is shown offline (10 to 3600; never less than the longest hold)"
            ),
            "validator": [int, MinValueValidator(10), MaxValueValidator(3600)],
            "default": 40,
        },
        # Fleet firmware updates (docs/fleet-updates.md)
        "FIRMWARE_REPO": {
            "name": _("Firmware repository"),
            "description": _(
                "The GitHub repository (owner/name) whose releases carry the scanner firmware"
            ),
            "default": "daveismith/inventree_nfc_scanner",
        },
        "FIRMWARE_CHECK_HOURS": {
            "name": _("Check for firmware every (hours)"),
            "description": _("How often to look for new releases; 0 only on demand"),
            "validator": [int, MinValueValidator(0), MaxValueValidator(720)],
            "default": 24,
        },
        "FIRMWARE_PRERELEASES": {
            "name": _("Include pre-releases"),
            "description": _("Fetch and offer releases marked as pre-releases"),
            "validator": bool,
            "default": False,
        },
        "FIRMWARE_AUTO_DEPLOY": {
            "name": _("Deploy new releases automatically"),
            "description": _(
                "A new stable release goes to every scanner running something older, as soon as it is fetched"
            ),
            "validator": bool,
            "default": False,
        },
        "FIRMWARE_USB_POLICY": {
            "name": _("USB update policy"),
            "description": _(
                "Whether the user at a USB scanner may put an update off; a deployment can override it"
            ),
            "choices": [
                ("deferrable", _("The user may choose later")),
                ("required", _("The update runs before the scanner can be used")),
            ],
            "default": "deferrable",
        },
        "FIRMWARE_MAX_DOWNLOADS": {
            "name": _("Network updates at once"),
            "description": _(
                "How many network scanners may download firmware at the same time; each holds a server worker"
            ),
            "validator": [int, MinValueValidator(1), MaxValueValidator(20)],
            "default": 2,
        },
        "FIRMWARE_KEEP": {
            "name": _("Firmware releases kept"),
            "description": _(
                "Images of older releases are deleted (their records stay); 0 keeps everything"
            ),
            "validator": [int, MinValueValidator(0), MaxValueValidator(100)],
            "default": 5,
        },
        "FIRMWARE_GITHUB_TOKEN": {
            "name": _("GitHub token"),
            "description": _(
                "Optional. Only to raise GitHub's rate limit, or for a private repository: a fine-grained, "
                "read-only token for that repository"
            ),
            "default": "",
            "protected": True,
        },
        "FIRMWARE_API": {
            "name": _("GitHub API"),
            "description": _(
                "Leave as it is, unless the releases are on GitHub Enterprise (or a test server)"
            ),
            "default": "https://api.github.com",
        },
    }

    SCHEDULED_TASKS = {
        "check_scanners": {"func": "check_scanners", "schedule": "I", "minutes": 1},
        "check_firmware": {"func": "check_firmware", "schedule": "I", "minutes": 60},
    }

    def __init__(self):
        """Say so once when the machine state cannot be shared between processes."""
        super().__init__()
        from django.conf import settings

        backend = settings.CACHES.get("default", {}).get("BACKEND", "")
        if "LocMemCache" in backend:
            import logging

            logging.getLogger("inventree").warning(
                "NFC scanner plugin: the cache is per process (%s). Scanner status is kept there, so the "
                "server and the worker will not agree and offline detection will not run. Configure "
                "InvenTree's global cache (Redis) for this plugin.",
                backend,
            )

    def check_scanners(self):
        """Mark scanners that have gone quiet as offline (runs every minute)."""
        from .sync import expire_jobs, mark_stale_scanners

        mark_stale_scanners(int(self.get_setting("SCANNER_OFFLINE_S") or 40))
        expire_jobs()

        from .fleet import check_deployments

        check_deployments()

    def check_firmware(self):
        """Look for new firmware releases, as often as the setting says (runs hourly)."""
        import datetime

        from django.utils import timezone

        from .firmware import last_check
        from .fleet_views import run_check

        hours = int(self.get_setting("FIRMWARE_CHECK_HOURS") or 0)
        if hours <= 0:
            return
        last = last_check()
        if last and last.get("at"):
            at = datetime.datetime.fromisoformat(last["at"])
            if timezone.now() - at < datetime.timedelta(hours=hours, minutes=-5):
                return
        run_check()

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

        from . import fleet_views, views

        return [
            path("sync/", views.SyncView.as_view(), name="sync"),
            path(
                "api/location/<int:pk>/tag/",
                views.LocationTagView.as_view(),
                name="location-tag",
            ),
            path(
                "api/location/<int:pk>/jobs/usb/",
                views.UsbJobView.as_view(),
                name="location-usb-job",
            ),
            path(
                "api/location/<int:pk>/link/",
                views.LinkView.as_view(),
                name="location-link",
            ),
            path("api/scanners/", views.ScannerListView.as_view(), name="scanners"),
            path("api/jobs/", views.JobListView.as_view(), name="jobs"),
            path("api/jobs/<int:pk>/", views.JobDetailView.as_view(), name="job"),
            path(
                "api/jobs/<int:pk>/cancel/",
                views.JobCancelView.as_view(),
                name="job-cancel",
            ),
            # Fleet updates
            path("api/fleet/", fleet_views.FleetView.as_view(), name="fleet"),
            path(
                "api/fleet/check/",
                fleet_views.FleetCheckView.as_view(),
                name="fleet-check",
            ),
            path(
                "api/fleet/upload/",
                fleet_views.FirmwareUploadView.as_view(),
                name="fleet-upload",
            ),
            path(
                "api/fleet/firmware/<int:pk>/",
                fleet_views.FirmwareDetailView.as_view(),
                name="fleet-firmware",
            ),
            path(
                "api/fleet/scanners/<str:reader>/",
                fleet_views.ScannerDetailView.as_view(),
                name="fleet-scanner",
            ),
            path(
                "api/fleet/deploy/",
                fleet_views.DeployView.as_view(),
                name="fleet-deploy",
            ),
            path(
                "api/fleet/deployments/",
                fleet_views.DeploymentListView.as_view(),
                name="fleet-deployments",
            ),
            path(
                "api/fleet/deployments/<int:pk>/cancel/",
                fleet_views.DeploymentCancelView.as_view(),
                name="fleet-deployment-cancel",
            ),
            path(
                "api/usb/checkin/",
                fleet_views.UsbCheckinView.as_view(),
                name="usb-checkin",
            ),
            path(
                "api/usb/deployments/<int:pk>/start/",
                fleet_views.UsbStartView.as_view(),
                name="usb-start",
            ),
            path(
                "api/usb/deployments/<int:pk>/defer/",
                fleet_views.UsbDeferView.as_view(),
                name="usb-defer",
            ),
            path(
                "api/usb/deployments/<int:pk>/report/",
                fleet_views.UsbReportView.as_view(),
                name="usb-report",
            ),
            path(
                "firmware/<str:version>/<str:name>",
                fleet_views.FirmwareImageView.as_view(),
                name="firmware-image",
            ),
        ]

    # User interface

    def panel_context(self, request, location_id):
        """What the panel needs to know up front."""
        return {
            "location": location_id,
            "api": "/plugin/nfcscanner/api/",
            "has_password": bool((self.get_setting("TAG_PASSWORD") or "").strip()),
            "job_timeout_s": int(self.get_setting("JOB_TIMEOUT_S") or 60),
            "can_program": bool(
                request.user and request.user.has_perm("stock.change_stocklocation")
            ),
        }

    def get_ui_panels(self, request, context: dict, **kwargs):
        """The "NFC tag" panel on a stock location's page."""
        context = context or {}
        if context.get("target_model") != "stocklocation" or not context.get(
            "target_id"
        ):
            return []
        return [
            {
                "key": "nfc-tag",
                "title": "NFC tag",
                "description": "Program the NFC tag on this bin",
                "icon": "ti:nfc:outline",
                "source": self.plugin_static_file("Panel.js:RenderNfcPanel"),
                "context": self.panel_context(request, context.get("target_id")),
            }
        ]

    def get_ui_dashboard_items(self, request, context: dict, **kwargs):
        """The scanners and what they last saw; for admins, their firmware too."""
        from .fleet_views import is_fleet_admin

        items = []
        if is_fleet_admin(getattr(request, "user", None)):
            items.append({
                "key": "nfc-fleet",
                "title": "NFC scanner firmware",
                "description": "Firmware on every scanner, and updates",
                "icon": "ti:nfc:outline",
                "source": self.plugin_static_file("Fleet.js:RenderNfcFleetItem"),
                "options": {"width": 6, "height": 4},
                "context": {"api": "/plugin/nfcscanner/api/"},
            })
        return items + [
            {
                "key": "nfc-scanners",
                "title": "NFC scanners",
                "description": "Network NFC scanners and their last tap",
                "icon": "ti:nfc:outline",
                "source": self.plugin_static_file(
                    "Dashboard.js:RenderNfcDashboardItem"
                ),
                "options": {"width": 3, "height": 2},
                "context": {"api": "/plugin/nfcscanner/api/"},
            }
        ]
