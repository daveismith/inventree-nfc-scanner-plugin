"""The NFC scanner as an InvenTree machine.

A network scanner is a machine of type `nfc-scanner` with the `nfc-network` driver. InvenTree
then provides the list of scanners, their settings, their status on the Machines page, and
their creation from the UI. The driver here does not talk to the scanner: the scanner talks
to the plugin (`/sync`), and the driver's job is to find the machine a call belongs to and
to keep its status current.
"""

from __future__ import annotations

import datetime

from django.utils.translation import gettext_lazy as _

from generic.states import ColorEnum
from machine.machine_type import BaseDriver, BaseMachineType, MachineStatus

MACHINE_TYPE = 'nfc-scanner'
NETWORK_DRIVER = 'nfc-network'

# Keys in the machine's shared state (visible to every server process).
STATE_LAST_SEEN = 'nfc_last_seen'  # ISO 8601 UTC of the last /sync
STATE_LAST_BOOT = 'nfc_last_boot'
STATE_LAST_TAG = 'nfc_last_tag'  # the last `tag` event outside a job, as a dict


class NfcScannerStatus(MachineStatus):
    """Status codes for an NFC scanner."""

    ONLINE = 100, _('Online'), ColorEnum.success
    UNKNOWN = 101, _('Unknown'), ColorEnum.secondary
    BUSY = 110, _('Busy'), ColorEnum.primary
    OFFLINE = 400, _('Offline'), ColorEnum.danger
    ERROR = 500, _('Error'), ColorEnum.danger


class NfcScannerBaseDriver(BaseDriver):
    """Base driver for NFC scanner machines."""

    machine_type = MACHINE_TYPE


class NfcScannerMachine(BaseMachineType):
    """An NFC reader/programmer for the tags on storage bins."""

    SLUG = MACHINE_TYPE
    NAME = _('NFC Scanner')
    DESCRIPTION = _('Reads and programs the NFC tags on storage bins.')

    base_driver = NfcScannerBaseDriver

    MACHINE_SETTINGS = {
        'LOCATION': {
            'name': _('Scanner location'),
            'description': _('Where this scanner sits (for information only)'),
            'model': 'stock.stocklocation',
        }
    }

    MACHINE_STATUS: type[NfcScannerStatus] = NfcScannerStatus
    default_machine_status = NfcScannerStatus.UNKNOWN

    # Shared-state helpers

    @property
    def last_seen(self) -> datetime.datetime | None:
        """When the scanner last called /sync."""
        value = self.get_shared_state(STATE_LAST_SEEN, None)
        return datetime.datetime.fromisoformat(value) if value else None

    def touch(self, boot: int) -> None:
        """Record a /sync call."""
        now = datetime.datetime.now(datetime.timezone.utc)
        self.set_shared_state(STATE_LAST_SEEN, now.isoformat())
        self.set_shared_state(STATE_LAST_BOOT, boot)

    @property
    def last_tag(self) -> dict | None:
        """The last tap reported outside a job."""
        return self.get_shared_state(STATE_LAST_TAG, None)

    def set_last_tag(self, tag: dict) -> None:
        """Remember a tap."""
        self.set_shared_state(STATE_LAST_TAG, tag)


class NetworkScannerDriver(NfcScannerBaseDriver):
    """A scanner on the network that polls InvenTree for jobs.

    Nothing to configure for the link: the scanner has the plugin's URL and a token, and
    calls in. The settings identify which scanner a call is from, and whose token it may use.
    """

    SLUG = NETWORK_DRIVER
    NAME = _('Network scanner')
    DESCRIPTION = _('A scanner on Wi-Fi that polls InvenTree for jobs over HTTPS')

    MACHINE_SETTINGS = {
        'READER_ID': {
            'name': _('Reader ID'),
            'description': _('The id the scanner reports, from its MAC address, e.g. nfc-34b7da52a084'),
            'required': True,
        },
        'USER': {
            'name': _('User'),
            'description': _('The user whose API token the scanner calls with'),
            'model': 'auth.user',
            'required': True,
        },
    }

    def init_machine(self, machine: BaseMachineType):
        """A machine starts offline until its scanner calls."""
        machine.set_status(NfcScannerStatus.OFFLINE)
        machine.set_status_text(str(_('Not heard from yet')))

    def find_by_reader_id(self, reader_id: str) -> NfcScannerMachine | None:
        """The active machine configured with this reader id, if any."""
        for machine in self.get_machines(active=True):
            if machine.get_setting('READER_ID', 'D') == reader_id:
                return machine
        return None
