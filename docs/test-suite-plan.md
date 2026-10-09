# Test suite: plan

Status: steps 1, 2, 3 and 6 built (2026-10-08; see [Progress](#progress)); the browser and firmware layers are next. The decisions are under [Decisions](#decisions). How to run and extend what exists: [tests/README.md](../tests/README.md).

A pytest suite for the plugin, run on GitHub's hosted runners against several InvenTree
versions, covering the server logic, the frontend in a headless browser with a simulated
scanner behind WebSerial, and the firmware in the loop. Every test runs without hardware,
without the internet once images are built, and without opening a port beyond the runner's
loopback or a private container network. Results show per test in the pull request.

It replaces `dev/check.py`, `dev/check_fleet.py` and `dev/check_rules.py`, which need a
running instance and a person to start them, and closes the open issues on automated tests and
on checking a firmware release's `min_plugin`.

## What InvenTree already provides

Verified against InvenTree 1.4.3 and 1.5.6:

- **pytest is InvenTree's own runner.** `pyproject.toml` configures pytest-django
  (`DJANGO_SETTINGS_MODULE = "InvenTree.settings"`, `pythonpath = src/backend/InvenTree`), and
  the dev requirements pin pytest and pytest-django. Settings treat a pytest run as test mode.
- **Plugins load in tests.** `INVENTREE_PLUGIN_TESTING` and `INVENTREE_PLUGIN_TESTING_SETUP`
  make the registry load plugins under test. `InvenTree.unit_test` has `PluginRegistryMixin`
  (`ensurePluginsLoaded`), `UserMixin` (`assignRole`, for the admin-role permission) and
  `registry.set_plugin_state(slug, True)`.
- **Machines load in tests.** InvenTree's own machine tests reload the plugin registry and call
  `machine.registry.initialize(main=True)`. Without it the registry is empty, as a management
  shell found.
- **Test mode differs from production in ways that matter here.** `USE_TZ` is off, and
  `DEFAULT_AUTO_FIELD` is `AutoField`, which is why `makemigrations` wants to alter the
  plugin's `BigAutoField` ids.
- **Docker images** are published for every release (`inventree/inventree:1.4.3`, `1.5.6`,
  `stable`, `latest`).
- **The plugin template** (`inventree/plugin-creator`) runs Playwright against a server
  started with `invoke dev.server` on the runner, with SQLite. It has no backend tests.

## Layers

| Layer | What it covers | Where it runs | InvenTree |
| --- | --- | --- | --- |
| 1. Pure | `ndef.py`, version ordering and manifest checks in `firmware.py` | any Python | none |
| 2. Server | `sync.py`, `fleet.py`, `firmware.py`, views, permissions, scheduled tasks, migrations | pytest-django, in process | source checkout per version |
| 3. Browser | the panel, dashboard, fleet page and USB update flow, with a simulated scanner | Playwright and InvenTree in containers | Docker image per version |
| 4. Firmware in the loop | the real firmware logic (`host_sim`) behind the browser, and as a network scanner | as 3, plus the simulator binary | as 3 |

Each layer is a pytest marker (`pure`, `server`, `browser`, `firmware`), so a job selects its
layer and a developer can run any of them locally.

## 1 and 2: the server suite

### Running against a version of InvenTree

As built (the first design checked out InvenTree's source per version; its Docker image turned
out to hold the same, with the requirements installed):

1. Build `inventree-nfc-test:<version>`, `FROM inventree/inventree:<version>` with the test
   packages added (`tests/Dockerfile`). In CI the layers are cached per version.
2. Run it with the working tree mounted, `--network none`, install the plugin from the tree,
   and run pytest with test settings (`tests/inventree_settings.py`): `tests/run.sh`.

The database is SQLite in a temporary directory on every push and pull request. A weekly
run (and any run started by hand) repeats the server suite against PostgreSQL, since production
uses it and the row locks (`select_for_update`) only mean something there. Tests that need real
row locks carry a `postgres` marker and are skipped on SQLite. For it, `tests/run.sh --db
postgres` puts the database and the tests on a Docker network created `--internal`: no route
off the host, nothing published.

### Fixtures (`tests/server/conftest.py`)

As designed; the fixtures as built, under the names tests use, are listed in
[tests/README.md](../tests/README.md).

| Fixture | Gives |
| --- | --- |
| `plugin` | the plugin loaded and active (`ensurePluginsLoaded`, `set_plugin_state`), its settings reset per test |
| `machines` | the machine registry initialised; a factory for NFC Scanner machines with a reader id and a user |
| `users` | an admin (superuser), a fleet admin (the admin role with change), a clerk (change on stock locations only), the scanner's user (no permissions), each with a token |
| `api` | DRF's `APIClient`, logged in as any of the above |
| `scanner` | a `FakeScanner` speaking `/sync`: numbers its messages, acknowledges commands, reports `fw`, and can restart (new `boot`) |
| `location`, `tag_uid` | a stock location; a fresh NTAG UID |
| `shared_cache` | parametrised: the per-process cache, and a shared one (fakeredis or `shared_cache()` patched), since some behaviour differs |
| `clock` | `time-machine`, for job timeouts, deployment timeouts, the late-`done` window and the release check interval |
| `github` | `responses`, serving a releases list and assets built from `make_release`-shaped fixtures, with digests to tamper with |
| `media` | a temporary `MEDIA_ROOT` |
| `no_sleep` | the long-poll loop's sleep patched out, with a hook to queue a command mid-hold |
| `notifications` | InvenTree's notification call recorded rather than sent |

### Rules

- **No network.** The container has none (`--network none`; with PostgreSQL, an internal
  network holding only the database), so a test that tried a real connection would fail.
  GitHub is always `responses`. pytest-socket, planned for this, is not needed.
- **Each test is independent.** The database rolls back after each test, and fixtures build
  only what a test asks for.
- **Time is never waited for.** It is moved with `clock`.

### What moves in

- **`dev/check.py`** (about 50 checks) and **`dev/check_fleet.py`** (about 60) become test modules:
  `test_sync.py`, `test_jobs.py`, `test_links.py`, `test_fleet_*.py`. Their scenarios carry over
  one to one, and are faster in process.
- **`dev/check_rules.py`** becomes real tests, with its rolled-back transaction replaced by the
  test database.
- **New coverage the live scripts could not reach:**
  - the deployment state machine's timeouts and retries;
  - `expire_jobs` and `mark_stale_scanners`;
  - concurrent deploy and delete, and concurrent links of one UID (`postgres`, weekly);
  - automatic deployment, which `--auto-deploy` could not isolate on a live server;
  - the plugin's settings validators.
- **`makemigrations --check`** for the plugin's app, once the ids are declared explicitly on
  the models.

## 3: the browser suite

### The stack, contained

A Compose file (`tests/browser/compose.yaml`) per run, all on one network declared
`internal: true`: no port is published, and nothing on it can reach the internet.

| Service | Image | Role |
| --- | --- | --- |
| `db` | `postgres` | InvenTree's database |
| `cache` | `redis` | the shared cache the plugin needs for scanner status |
| `inventree` | `inventree-nfc-test:<version>`, built `FROM inventree/inventree:<version>` with the plugin installed | the server, migrated and set up by an entrypoint script |
| `worker` | the same image | scheduled tasks |
| `proxy` | `caddy` | static and media files, as in production |
| `tests` | `mcr.microsoft.com/playwright/python` with the test requirements | pytest, Playwright and Chromium, the simulated scanner, and the stand-in for GitHub |

The only step that touches the internet is building the images, before the network exists.
Installing the plugin into the image at build time also removes the dev setup's restart
dance: the plugin is installed before the server first starts.

Chromium reaches InvenTree at `http://inventree`. That is not a secure context, and WebSerial,
Web Locks and `crypto.subtle` all need one. Chromium is started with
`--unsafely-treat-insecure-origin-as-secure=http://inventree` (or `proxy`), which makes the
origin behave as it does under https.

On a developer machine, the same Compose file runs with an override that publishes the proxy
on `127.0.0.1` only, for watching a test in a headed browser.

### Simulating the scanner behind WebSerial

Chromium cannot be given a fake serial device, so the test replaces the API. Before any page
script runs, an init script (`page.add_init_script`) defines `navigator.serial`. It is a small
object implementing what `frontend/src/serial.ts` uses:

- `getPorts()`, `requestPort()`;
- the `connect` and `disconnect` events, dispatched with the port as their target, as the
  specification has them;
- a port with `open()`, `close()`, `setSignals()`, `getInfo()` (`303a:4e46`), and `readable` and
  `writable` streams.

The streams are bridged to Python through `page.expose_function`:

- **Writes.** The writable stream's sink calls `__serial_write(bytes)`, which hands the bytes to
  the scanner model.
- **Reads.** The readable stream's `pull()` calls `__serial_read()`, which returns whatever the
  model has sent since, or nothing. On nothing, the stream waits ten milliseconds and asks
  again. This keeps pytest-playwright's synchronous API usable without threads.
- **Unplug and replug.** Python can make the port vanish (the reader ends, `disconnect`
  fires) and come back (`connect` fires, `getPorts()` returns it). That is what a firmware
  update's restart looks like to the page.

Behind the bridge sits a **scanner model** with one interface and two implementations:

- **`FakeScanner` (Python).** The protocol from `components/proto`, with state a test can set:
  a tag present or not, a tag's contents and protection, errors to inject, keyboard output on
  or off. It also implements `ota_begin`/`ota_data`/`ota_end`, checking size and sha256, and
  "restarts" under the new version. It is fast and deterministic, and it is what most browser
  tests use.
- **`SimScanner` (layer 4).** The firmware's own `host_sim` on a pty: the bridge copies bytes
  between the page and the pty, so the browser talks to the real state machine, protocol and
  tag logic.

`FakeScanner` is kept honest by running the protocol tests of layer 4 against both
implementations.

### Fixtures

| Fixture | Gives |
| --- | --- |
| `stack` (session) | the Compose project up and healthy; torn down at the end, with its logs saved |
| `browser_context_args` | Chromium with the secure-origin flag at 1280×720; tracing, screenshots and video kept on failure, the video turned into a GIF (below) |
| `serial` | the init script installed in the page, wired to a scanner model; `serial.scanner` to drive it, `serial.unplug()` and `serial.replug()` |
| `logged_in` | a page logged in as admin, clerk or a user without permissions (InvenTree's own Playwright fixtures do form login the same way) |
| `seed` | data through the REST API: locations, network machines, releases, deployments |
| `github` | the stand-in for GitHub's API, inside the `tests` container, set as the plugin's *GitHub API* setting |

### Scenarios

- **Location panel, USB.**
  - Connect, then program a blank tag.
  - Program a tag that is not blank: the existing text is shown, then overwrite.
  - A protected tag.
  - Unplug mid-job.
  - The barcode link: done, and moved from another bin.
- **Location panel, network.** A job on a network scanner, played by the server-side
  `FakeScanner` through `/sync`. The busy (409) message.
- **Dashboard.** Scanner online and offline; "USB here" on the scanner that is plugged in;
  the last tap.
- **USB update.**
  - The offer, "Later" and "Update now": progress, the restart (unplug and replug), and
    confirmation.
  - Required: installs at once, with no "Later".
  - A required-from date.
  - The image refused (wrong sha256): the note, and the offer returning.
  - The browser closed mid-transfer: offered again next time.
- **Fleet page (admins only).**
  - Not shown to a clerk.
  - Check now, against the stand-in.
  - Upload: good, and tampered.
  - Delete: the row goes.
  - Deploy to chosen and to all, the downgrade confirmation, withdraw, forget.
  - The history.
- **Tap to navigate.** A tap on another bin's tag opens that bin.

Each scenario checks what the user sees (text, badges, buttons enabled) and what the server
recorded (through the API), not internal state.

## 4: firmware in the loop

The firmware repository's release workflow also publishes the simulator as an asset
(`inventree_nfc_scanner-<version>-sim-linux-x86_64`), and CI uploads it as an artifact on
every push. The plugin's jobs download the simulator of:

- the newest firmware release;
- the oldest release this plugin claims to drive;
- (from the firmware repository's CI) the commit under test.

With it:

- **Browser.** A subset of the browser scenarios runs with `SimScanner` behind WebSerial:
  programming, a refused job, a USB update.
- **Network.** `host_sim` runs as a network scanner (`SIM_SYNC_URL` pointing at the stack)
  through the contract scenarios in the firmware's `docs/compat-testing-plan.md`. Run from the
  firmware repository against the plugin at its `min_plugin`, this is the release gate that
  plan describes.

## The firmware repository's tests in pytest

They run in the `espressif/idf` container, as now, and nothing in them leaves the container:
the fake plugin listens on its loopback.

- **Unity host tests.** A pytest plugin (`conftest.py`) runs `host_test.elf`, parses Unity's
  per-test lines, and reports each Unity test as a pytest item, so the 86 show individually.
- **Simulator and sync checks.** `tools/test_sim.py` and `tools/test_sync.py` become pytest
  modules. A `sim` fixture starts the simulator on a pty, a `fake_plugin` fixture starts the
  stand-in server, and each current `check(...)` becomes a test.
- **Release packaging.** `tools/make_release.py` gets tests of its own: the tag check, refusing
  development builds, and the manifest's fields.

## Versions of InvenTree

| Leg | Versions | When |
| --- | --- | --- |
| Supported | 1.4.3 and 1.5.6: the oldest supported, and the newest patch of each minor since | every push and pull request; blocking |
| Next | `latest` image / `master` source | nightly, and on demand; not blocking; a failure opens or updates an issue |

- **The list lives in one file** (`tests/inventree-versions.json`), read by the workflow's
  matrix.
- **A weekly job proposes new releases.** It compares the list with InvenTree's releases and
  opens a pull request adding a new release, so a version is supported once it passes.
- **`MIN_VERSION` matches the oldest version tested.** It is 1.4.3 (it was 1.0.0, which
  claimed more than had ever been run). When the oldest version leaves the list, `MIN_VERSION`
  rises with it in the same pull request. A test reads both and fails if they disagree.

## Reporting in GitHub

- **Per test, in the pull request.** Every job writes JUnit XML with the InvenTree version in
  the suite name. A final job publishes all of them as one check run, with a table per layer
  and version, and failures listed with their message. The publishing action
  (`EnricoMi/publish-unit-test-result-action`, pinned by hash like the others) needs
  `checks: write`. For pull requests from forks, which get a read-only token, it runs from a
  `workflow_run` workflow instead.
- **Annotations.** `pytest-github-actions-annotate-failures` puts each failure on the line of
  the test that failed, in the Files view.
- **Job summary.** A conftest hook writes a short Markdown summary to `$GITHUB_STEP_SUMMARY`: the
  counts per layer and version, the slowest tests, and links to the artifacts below.
- **Browser failures.** The Playwright trace, a screenshot and an animated GIF of the failing
  test, the scanner model's transcript of every line exchanged, and the stack's container logs
  are uploaded as artifacts, kept 14 days. A trace opens in `playwright show-trace`, or at
  trace.playwright.dev.
- **Video as GIF.** pytest-playwright records with `--video retain-on-failure`, so passing tests
  leave nothing. A hook after each failed test converts its WebM to a GIF with ffmpeg (installed
  in the `tests` image) and deletes the WebM:

  ```
  ffmpeg -i video.webm -vf "fps=6,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" video.gif
  ```

  Six frames a second, 800 pixels wide and a 64-colour palette suit a mostly static page: the
  palette is computed per clip, and only the changed rectangle of each frame is stored. A
  ten-second clip that changes in every pixel, the worst case, came to 1.4 MB; a page that
  mostly holds still comes to much less. If a GIF still exceeds 5 MB, the hook halves
  the frame rate and tries once more, and otherwise keeps the trace alone, which holds the
  same frames as screenshots.
- **Coverage.** pytest-cov on layer 2, with the total in the summary and the report as an
  artifact. A coverage floor is added once the suite has settled.

## The workflows

| Workflow | Jobs | Trigger |
| --- | --- | --- |
| `ci.yaml` (existing) | lint, build, frontend lint and build, bundle check | push, pull request |
| `tests.yaml` | `pure`; `server` × version (SQLite); `browser` × version; `firmware` (newest and oldest release); `report` | push, pull request |
| `scheduled.yaml` | nightly: the suite against InvenTree `master`/`latest`. Weekly: `server` × version on PostgreSQL, and the version-list proposal | schedule, manual |

Images are cached between runs: the InvenTree base images through the Docker layer cache, the
test images as build artifacts keyed by InvenTree version and the plugin's requirements, and
pip by InvenTree tag.

Rough run time on a hosted runner: server about 4 minutes per leg, browser about 8 per
version, in parallel, so about 10 minutes for a pull request (two server legs and two browser
legs). The browser stack uses PostgreSQL throughout, as production does, so a pull request
still exercises PostgreSQL through the UI; only the server suite's PostgreSQL legs wait for
the weekly run.

## Order of work

1. **Spike, about half a day.**
   - The plugin loaded and active under pytest-django in InvenTree 1.4.3 and 1.5.6, with the
     machine registry initialised and one `/sync` test passing.
   - Decide here whether `INVENTREE_PLUGIN_TESTING_SETUP` is needed for the plugin's models to
     migrate in the test database.
2. **The server suite.**
   - Port `check.py`, `check_fleet.py` and `check_rules.py`, and add the coverage listed above.
   - The weekly PostgreSQL legs, and the `postgres` marker.
   - Fix the model ids so `makemigrations --check` can run.
3. **Reporting**, so everything after shows in pull requests from the start.
4. **The browser stack and the WebSerial shim, with `FakeScanner`.** A smoke test (log in, open
   a location, connect, program a tag), then the scenarios, then the GIF conversion.
5. **The firmware side.**
   - Its tests in pytest.
   - The simulator published by its release workflow and CI.
   - Firmware in the loop here; the contract gate there.
6. **The nightly run against InvenTree's next release, and the version-list proposal.**
7. **Retire the `dev/check*.py` scripts.** Point `dev/README.md` at `pytest -m server` (in
   process) and at the browser stack.

## Progress

- **1. Spike: done.** Four things make InvenTree testable under pytest, all in `tests/`:
  "test" added to its command line (`inventree_setup.py`; otherwise it checks migrations on
  the configured database and exits), the plugin's app installed in the settings before the
  test database is migrated (`inventree_settings.py`), time zones on as in production, and
  the registries trimmed for speed (`server/conftest.py`: no sample plugins, no registry hash).
  `INVENTREE_PLUGIN_TESTING_SETUP` is needed: without it, plugins are not loaded from entry
  points under test.
- **2. Server suite: done.** 194 tests (about 40 s a version), ported from `dev/check.py`,
  `dev/check_fleet.py` and `dev/check_rules.py` with the coverage listed above. The model ids
  are declared, so `makemigrations --check` runs. The first PostgreSQL run found the
  concurrent-link race (open issue 6 then), now fixed.
- **3. Reporting: done**, with one difference: the job summary is the publishing action's and
  the coverage table, not a conftest hook of our own. Coverage is combined across the legs;
  the HTML report is the `coverage-html` artifact.
- **6. Nightly and version proposal: done** (`.github/workflows/scheduled.yaml`). A pull
  request it opens does not start the tests by itself (GitHub's rule for its own token):
  close and reopen it.
- **4, 5 and 7: not started.** `dev/check*.py` stay until the browser stack exists.

## Decisions

Settled on 2026-10-08:

- **Versions.** InvenTree 1.4.3 and 1.5.6. `MIN_VERSION` rises from 1.0.0 to 1.4.3.
- **PostgreSQL** runs weekly and on demand. Pull requests run the server suite on SQLite, and
  the browser suite on PostgreSQL.
- **Video** is kept for failed tests only, as an animated GIF.
