"""Fixtures for the server suite: InvenTree in process, with the plugin loaded and active.

The database is pytest-django's test database. What the session sets up (the plugin's
registration, the base URL) is created once, outside any test's transaction; everything a test
makes is rolled back after it.
"""

from __future__ import annotations

import itertools

import pytest

from .scanner import P, SLUG, FakeScanner

BASE_URL = "https://inv.example.com"
HOST = "inventree.test"

_counter = itertools.count(1)


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """The test database, with the plugin registered and active, as an admin would leave it."""
    with django_db_blocker.unblock():
        from common.models import InvenTreeSetting
        from plugin import registry

        # Under test InvenTree also loads its ~40 sample and testing plugins, and recomputes a
        # hash over every plugin on each machine change; without them a test runs several
        # times faster. What is left is what production loads: the built-ins and this plugin.
        registry.plugin_dirs = lambda: ["plugin.builtin"]
        registry.reload_plugins(full_reload=True, force_reload=True, collect=True)
        registry.set_plugin_state(SLUG, True)
        assert registry.get_plugin(SLUG), "the plugin did not load"
        InvenTreeSetting.build_default_values()
        InvenTreeSetting.set_setting("INVENTREE_BASE_URL", BASE_URL, None)

        from machine import registry as machines

        machines.initialize(main=True)

        # A transactional test (tests/server/test_concurrency.py) empties the database after it
        # and restores what was serialised when the test database was made: before the above.
        # Serialised again, so the plugin's registration and settings come back too.
        from django.db import connection

        if connection.vendor == "postgresql":
            connection._test_serialized_contents = (
                connection.creation.serialize_db_to_string()
            )
        # The hash tells other server processes to reload their machines; a test run is one
        # process, and recomputing it on every machine change costs a second a test. Checking
        # it goes too: a reload empties the registry first, which threads of one process (the
        # concurrency tests; InvenTree serves a request per process) would see half done.
        machines._update_registry_hash = lambda *a, **k: None
        machines._check_reload = lambda *a, **k: None


@pytest.fixture(autouse=True)
def fresh_cache():
    """Machine state and the last release check live in the cache, keyed by ids the rolled
    back database hands out again: each test starts with it empty."""
    from django.contrib.contenttypes.models import ContentType
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
    # Emptied and refilled by a transactional test, the content types may have new ids.
    ContentType.objects.clear_cache()


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
        username=f"{name}-{next(_counter)}",
        password="x",
        is_superuser=superuser,
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
        client = APIClient(SERVER_NAME=HOST, HTTP_ACCEPT="application/json")
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
def machines(db):
    """The machine registry (loaded once for the session); the machines a test adds to it are
    taken out again afterwards, since the database forgets them."""
    from machine import registry

    before = set(registry.machines)
    yield registry
    for pk in set(registry.machines) - before:
        del registry.machines[pk]


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
        if (
            machines.get_machine(config.pk) is None
        ):  # saving it may have added it already
            machines.add_machine(config, initialize=True, update_registry_hash=False)
        machine = machines.get_machine(config.pk)
        machine.set_setting("READER_ID", "D", reader or f"nfc-{next(_counter):012x}")
        machine.set_setting("USER", "D", str((user or nobody).pk))
        return machine

    return make


@pytest.fixture
def scanner(network_scanner, api, nobody):
    """A network scanner and its machine (`scanner.machine`), calling with its user's token."""
    machine = network_scanner(user=nobody)
    return FakeScanner(
        api(nobody), reader=machine.get_setting("READER_ID", "D"), machine=machine
    )


@pytest.fixture
def job_for(api, clerk, location):
    """`job_for(scanner, **fields)`: queue a job through the API, as the panel does."""

    def make(scanner, **fields):
        r = api(clerk).post(
            f"{P}/api/jobs/",
            {"location": location.pk, "scanner": str(scanner.machine.pk), **fields},
            format="json",
        )
        assert r.status_code == 201, r.json()
        return r.json()

    return make


# Firmware ---------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    """Uploaded and fetched images go to a directory of the test's own."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    return tmp_path / "media"


@pytest.fixture
def fleet(api, admin_user):
    """An API client for the fleet pages, as an admin."""
    return api(admin_user)


@pytest.fixture
def github(set_setting):
    """A stand-in for GitHub's API, set as the plugin's: `github.publish(release(...))`."""
    import responses

    from .releases import FakeGitHub

    set_setting("FIRMWARE_REPO", FakeGitHub.REPO)
    set_setting("FIRMWARE_API", FakeGitHub.API)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield FakeGitHub(mock)


@pytest.fixture
def notifications(monkeypatch):
    """InvenTree's notifications, recorded rather than sent: a list of (category, targets, context)."""
    import common.notifications

    sent = []
    monkeypatch.setattr(
        common.notifications,
        "trigger_notification",
        lambda obj, category, targets=None, context=None, **kw: sent.append((
            category,
            targets,
            context,
        )),
    )
    return sent
