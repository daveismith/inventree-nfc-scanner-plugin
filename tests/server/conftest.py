"""Fixtures for the server suite: InvenTree in process, with the plugin loaded and active.

The database is pytest-django's test database. What the session sets up (the plugin's
registration, the base URL) is created once, outside any test's transaction; everything a test
makes is rolled back after it.
"""

from __future__ import annotations

import itertools

import pytest

from .scanner import SLUG, FakeScanner

BASE_URL = "https://inv.example.com"
HOST = "inventree.test"

_counter = itertools.count(1)


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """The test database, with the plugin registered and active, as an admin would leave it."""
    with django_db_blocker.unblock():
        from common.models import InvenTreeSetting
        from plugin import registry

        registry.reload_plugins(full_reload=True, force_reload=True, collect=True)
        registry.set_plugin_state(SLUG, True)
        assert registry.get_plugin(SLUG), "the plugin did not load"
        InvenTreeSetting.build_default_values()
        InvenTreeSetting.set_setting("INVENTREE_BASE_URL", BASE_URL, None)


@pytest.fixture
def plugin(db):
    """The running plugin; its settings go back to their defaults after the test."""
    from plugin import registry

    return registry.get_plugin(SLUG)


@pytest.fixture
def set_setting(plugin):
    """Change one of the plugin's settings for this test (rolled back with the database)."""

    def set_(key, value):
        plugin.set_setting(key, value)

    return set_


# Users ------------------------------------------------------------------------------------


def _user(name, *, superuser=False, roles=()):
    """A user, in a group of their own with the given InvenTree roles ("stock_location.change")."""
    from django.contrib.auth.models import Group, User

    user = User.objects.create_user(
        username=f"{name}-{next(_counter)}", password="x", is_superuser=superuser,
        is_staff=superuser,
    )
    if roles:
        group = Group.objects.create(name=f"{user.username}-group")
        for role in roles:
            rule, perm = role.split(".")
            ruleset = group.rule_sets.get(name=rule)
            setattr(ruleset, f"can_{perm}", True)
            ruleset.save()
        user.groups.add(group)
    return user


@pytest.fixture
def admin_user(db):
    """A superuser."""
    return _user("admin", superuser=True)


@pytest.fixture
def fleet_admin(db):
    """Not a superuser, but may change machines: the fleet pages' permission."""
    return _user("fleet", roles=("admin.change", "stock_location.change"))


@pytest.fixture
def clerk(db):
    """May change stock locations (so program tags), nothing else."""
    return _user("clerk", roles=("stock_location.view", "stock_location.change"))


@pytest.fixture
def nobody(db):
    """A user with no permissions at all; a network scanner's user is one."""
    return _user("nobody")


def token_for(user) -> str:
    """A new API token for the user."""
    from users.models import ApiToken

    return ApiToken.objects.create(user=user, name=f"test-{next(_counter)}").key


@pytest.fixture
def api():
    """An API client factory: `api(user)` is logged in with a token of theirs, `api()` is not."""
    from rest_framework.test import APIClient

    def make(user=None, token=None):
        client = APIClient(SERVER_NAME=HOST)
        if user is not None:
            token = token or token_for(user)
        if token:
            client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        return client

    return make


# Stock and machines -----------------------------------------------------------------------


@pytest.fixture
def location(db):
    """A fresh stock location (a bin)."""
    return make_location()


def make_location(name=None):
    from stock.models import StockLocation

    return StockLocation.objects.create(name=name or f"Bin {next(_counter)}")


@pytest.fixture
def tag_uid():
    """A fresh 7-byte NTAG UID, as hex."""
    return lambda: f"04{next(_counter):012X}"


def reload_machines():
    from machine import registry

    registry.initialize(main=True)


@pytest.fixture
def machines(db):
    """The machine registry, reloaded from this test's database before and after it."""
    from machine import registry

    reload_machines()
    yield registry
    reload_machines()


@pytest.fixture
def network_scanner(machines, nobody):
    """`network_scanner(reader=..., user=...)`: an active network scanner machine."""
    from machine.models import MachineConfig

    def make(reader=None, user=None, name=None, active=True):
        config = MachineConfig.objects.create(
            name=name or f"Scanner {next(_counter)}",
            machine_type="nfc-scanner",
            driver="nfc-network",
            active=active,
        )
        reload_machines()
        machine = machines.get_machine(config.pk)
        machine.set_setting("READER_ID", "D", reader or f"nfc-{next(_counter):012x}")
        machine.set_setting("USER", "D", str((user or nobody).pk))
        return machine

    return make


@pytest.fixture
def scanner(network_scanner, api, nobody):
    """A network scanner and its machine (`scanner.machine`), calling with its user's token."""
    machine = network_scanner(user=nobody)
    return FakeScanner(api(nobody), reader=machine.get_setting("READER_ID", "D"), machine=machine)
