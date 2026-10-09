# Tests

The plugin's test suite: pytest, against every InvenTree version the plugin supports, with no
hardware and no network. The design and what is still to come (a headless browser with a
simulated scanner, the firmware in the loop) are in
[docs/test-suite-plan.md](../docs/test-suite-plan.md).

| Layer | Where | Needs | Run with |
| --- | --- | --- | --- |
| pure | `tests/pure/` | Python and pytest | `pytest -m pure` |
| server | `tests/server/` | Docker | `tests/run.sh` |

## Running

```sh
tests/run.sh                                  # pure and server, against the oldest supported InvenTree
tests/run.sh --version 1.5.6                  # another version (an inventree/inventree image tag)
tests/run.sh --db postgres                    # PostgreSQL, which also runs the concurrency tests
tests/run.sh --no-build tests/server/test_jobs.py -k cancel -x   # skip the image check; any pytest arguments
```

The first run of a version builds `inventree-nfc-test:<version>` (a few minutes); after that a
run takes about 40 seconds, 30 of them migrating the test database. The working tree is
mounted, so a change to the plugin or a test needs no rebuild. `--no-build` skips even
checking the image is current; drop it after changing `tests/Dockerfile` or
`tests/requirements.txt`.

Results go to `test-results/`, which git ignores. For coverage, as CI measures it:

```sh
COVERAGE_FILE=test-results/coverage.dat tests/run.sh --cov --cov-report=html:test-results/coverage-html
```

The pure layer also runs without Docker: `pip install pytest` and `pytest -m pure`.

## What is where

- `inventree-versions.json`: the versions CI tests, oldest first. The oldest is the plugin's
  `MIN_VERSION`; `test_versions.py` fails if they disagree.
- `Dockerfile`, `requirements.txt`, `run.sh`: the test image and the runner.
- `inventree_setup.py`, `inventree_settings.py`: what makes InvenTree testable under pytest
  (below).
- `conftest.py`: a test's layer marker comes from its directory; `postgres` tests are skipped
  unless the run is against PostgreSQL.
- `server/conftest.py`: the fixtures. `server/scanner.py`: `FakeScanner`, a network scanner as
  the plugin sees it. `server/releases.py`: made-up firmware releases, and a stand-in for
  GitHub's API.
- `propose_versions.py`: the weekly proposal of new InvenTree versions (CI).

## Fixtures (`server/conftest.py`)

| Fixture | Gives |
| --- | --- |
| `plugin`, `set_setting` | the running plugin; `set_setting(key, value)` for this test only |
| `admin_user`, `fleet_admin`, `clerk`, `nobody` | a superuser; the admin role with change (the fleet pages' permission); stock location view and change (may program tags); no permissions (as a network scanner's user) |
| `api` | `api(user)`: a DRF client with a token of theirs; `api()`: not logged in; `api(token=...)` |
| `location` | a fresh stock location; `make_location()` for more |
| `machines`, `network_scanner` | the machine registry; `network_scanner(reader=, user=, active=)` makes an NFC scanner machine |
| `scanner` | a `FakeScanner` with its machine (`scanner.machine`) and its user's token |
| `job_for` | `job_for(scanner, overwrite=...)`: a job queued through the API, as the panel does |
| `fleet` | an API client as an admin, for the fleet pages |
| `github` | `responses` standing in for GitHub, set as the plugin's repository and API: `github.publish(release("1.1.0"), prerelease=..., bad_digest=...)` |
| `notifications` | InvenTree's notifications, recorded instead of sent |
| `media` (automatic) | a media directory of the test's own |
| `time_machine` | from time-machine: `time_machine.move_to(...)` for timeouts |

`FakeScanner.sync(*msgs, **body)` makes one `/sync` call and returns `(status, body)`;
messages are numbered for it. `cmds()` does the same and returns the commands, insisting on
a 200. `ack(cmd)` acknowledges with the next call; `restart()` starts a new boot.

Each test runs in a transaction rolled back after it, and the cache is cleared around it, so
tests do not see each other. What the session sets up (the plugin registered and active, the
base URL `http://inventree.test`) is there for all of them.

## Writing a test

- Go through the API where the browser or the scanner would: `api(clerk).post(...)`,
  `scanner.sync(...)`. Call the plugin's functions directly only for what no request reaches
  (`expire_jobs()`, `check_deployments()`, `plugin.check_scanners()`).
- Never wait. Move the clock with `time_machine`; the long-poll hold has the `hold` fixture
  in `test_sync.py`, which runs the hold on a virtual clock.
- A test that needs two requests at once (row locks) goes in `test_concurrency.py`: it is
  transactional, marked `postgres`, and runs only with `--db postgres`.
- `ruff check` and `ruff format --preview` cover the tests too (CI checks them).

## How InvenTree is made testable

Each of these is commented where it is done; together they are what the spike found.

- **"test" on the command line** (`inventree_setup.py`, loaded with `-p` before
  pytest-django starts Django). InvenTree's start-up otherwise checks for migrations on the
  configured database, finds none run, and exits.
- **The plugin's app in the settings** (`inventree_settings.py`). Under test, plugins load
  only after the test database is migrated, too late for the plugin's tables.
- **`INVENTREE_PLUGIN_TESTING_SETUP`** (`Dockerfile`): without it, plugins are not loaded from
  their entry points under test.
- **Time zones on** (`inventree_settings.py`): InvenTree turns them off under test; production
  has them on, and the plugin stores aware times.
- **No site URL** (`Dockerfile`): `INVENTREE_SITE_URL` would override the base URL setting,
  which tests change.
- **Smaller registries** (`server/conftest.py`): no sample plugins, and no registry hash, which
  is for other server processes and cost a second a test.

## In CI

- `.github/workflows/tests.yaml`, on every push and pull request: a leg per supported version
  on SQLite, then one job that publishes the results as the **Test results** check run (with a
  comment on the pull request when results change), and combines the coverage. Failures are
  annotated on the failing line. Each run's summary shows the coverage table; the full HTML
  report is the run's **coverage-html** artifact, and each leg's JUnit XML and coverage data
  are its **results-…** artifacts (14 days).
- `.github/workflows/scheduled.yaml`: weekly, the same against PostgreSQL (**Test results
  (PostgreSQL)**); nightly, against InvenTree's `latest` image, opening or updating an issue
  when it fails; weekly, a pull request when InvenTree releases a new minor. Any of them can be
  started by hand from the Actions tab.

## Adding an InvenTree version

Add it to `inventree-versions.json` (the weekly job proposes new minors). To drop the oldest,
remove it there and raise `MIN_VERSION` in `inventree_nfc_scanner/core.py` to the new oldest,
in the same change.
