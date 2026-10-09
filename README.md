# InvenTree NFC Scanner plugin

An [InvenTree](https://inventree.org) plugin that programs the NFC tags on storage bins.
Each bin is a stock location; its tag holds the location's page URL and InvenTree's short
barcode for it, and the tag's UID is linked to the location as a barcode. The hardware, a
desk scanner/programmer, is the
[inventree_nfc_scanner](https://github.com/daveismith/inventree_nfc_scanner) firmware.

Two routes to a tag, behind one "Program tag" button on every stock location's page:

- **USB.** The scanner is plugged into the user's computer and the browser drives it over
  WebSerial (Chrome or Edge). The server supplies what to write and records the outcome.
- **Network.** The scanner is on Wi-Fi and polls this plugin for jobs. For browsers without
  WebSerial, computers without the scanner, and headless scanners in automated storage.
  Each network scanner is an InvenTree *machine*.

Admins can keep every scanner's firmware current from InvenTree: releases are fetched from the
firmware's GitHub releases, and a deployment reaches a network scanner on its next call and a
USB scanner the next time a browser connects to it. See
[docs/fleet-updates.md](docs/fleet-updates.md).

Looking a bin up needs no plugin: the desk scanner types the location's barcode into
InvenTree's scan dialog as a keyboard.

Needs InvenTree 1.4.3 or later (the machine framework and the plugin UI of 1.x); tested
against 1.4.3 and 1.5.6 (`tests/inventree-versions.json`).

Known gaps are listed in [docs/open-issues.md](docs/open-issues.md).

## Install on the server

InvenTree in Docker, as on the Raspberry Pi:

1. **Put the package in `plugins.txt`** in the data volume (next to `config.yaml`, usually
   `inventree-data/plugins.txt`). One line, either a release from PyPI once there is one,
   or the repository directly:

   ```
   git+https://github.com/daveismith/inventree-nfc-scanner-plugin.git
   ```

   InvenTree installs everything in that file at each start (`INVENTREE_PLUGINS_ENABLED`
   must be `True`, which the standard `.env` sets). Alternatively, *Admin Center → Plugins
   → Install Plugin* with the same package name, which writes that line for you.

2. **Restart the server and worker:** `docker compose restart inventree-server inventree-worker`.
   The plugin is installed but not yet active.

3. **Turn on the plugin integrations** under *Admin Center → Settings → Plugin Settings*:
   URL integration, app integration, interface integration and schedule integration. Set the global *Base URL* to the address users reach InvenTree
   at (`https://inventree.example`): it goes onto every tag.

4. **Activate the plugin** under *Admin Center → Plugins*, then restart the server and
   worker once more: an active plugin with database models needs that for its tables and
   static files.

5. **Settings**, under the plugin's entry: the *Tag password* (eight hex digits; tags are
   write-protected with it after programming; blank leaves them open), the *Tag password
   acknowledge (PACK)*, the *Job timeout*, *Long polling* with its *Longest hold* (off by
   default; see below), and *Offline after*, how long a quiet scanner is shown online.

   The tag password is one secret shared by every tag the server programs, and every user
   who may program tags receives it, since their scanner needs it to write the tag. Give
   the stock-location change permission with that in mind.

   **Set the tag password once, before programming tags, and leave it.** The plugin writes
   with the current password only: a tag protected with an earlier one cannot be reprogrammed
   or wiped from InvenTree once the setting changes (a known gap; see docs/open-issues.md). Each network scanner should
   have an InvenTree user of its own: a token serves every scanner configured with its
   user, and the dashboard warns when two share one.

   The plugin keeps each scanner's status in InvenTree's cache, which must be the shared
   one (Redis, as in InvenTree's own Docker setup): the server's workers and the background
   worker each see their own copy otherwise, so the dashboard's status depends on which
   worker answers, offline detection does not run, and the network route is unreliable.
   With the per-process fallback the plugin logs a warning at start.

`dev/setup.sh` does steps 3 and 4 through the API for the local instance; the same calls
work against any server.

### A network scanner

1. *Admin Center → Machines → Add*: type *NFC Scanner*, driver *Network scanner*. Set its
   *Reader ID* to what the scanner reports (`nfc-` and its MAC in lower-case hex, e.g.
   `nfc-34b7da52a084`). The scanner says it in its `info` and `hello` (`reader`), and
   `nfcprog.py net` shows it; the dashboard's fleet page lists it once the scanner has been
   connected to a browser. The USB serial number is the same MAC in upper case without the
   prefix (`34B7DA52A084`), which does not match.
2. Make a user for the scanner (one per scanner, so a lost one can be revoked alone), with
   no permissions beyond logging in, and set it as the machine's *User*.
3. Log in as that user and create an API token (*user settings → API tokens*). Give the
   scanner the plugin's URL and the token, over USB, with the firmware's `net` command.

The machine shows *Online* on the Machines page once the scanner has called in.

### Long polling

With *Long polling* on, a scanner's request is held until a job is queued for it, so jobs
start at once. Each held request occupies a web worker thread for up to *Longest hold*
seconds.
Off, scanners poll once a second and a job starts within that. On a Raspberry Pi with few
gunicorn workers, leave it off unless there is one scanner and the workers have been counted.

## Build

The Python package needs no build. The browser side does, and the built files
(`inventree_nfc_scanner/static/`) are committed so that an install straight from the
repository works. Rebuild them, and commit the result, after any change under `frontend/`:

```sh
cd frontend
npm install
npm run build          # writes inventree_nfc_scanner/static/
```

Then, for a release: `python -m build` in the repository root makes the wheel and sdist
(`pip install build` first). The GitHub workflow in `.github/workflows/pypi.yaml` publishes
when a GitHub release is published. CI checks that the committed bundle in
`inventree_nfc_scanner/static/` is what the sources build, so run `npm run build` and commit
its output with any frontend change.

### Frontend development

`npm run dev` in `frontend/` serves the components with hot reloading on port 5174. Point
InvenTree at it with, in its `config.yaml`:

```yaml
plugin_dev:
  slug: nfcscanner
  host: http://localhost:5174
```

## Develop and test locally

`tests/run.sh` runs the server tests in Docker against a supported InvenTree version, with no
network, and `tests/browser/run.sh` the browser tests; CI runs both against every supported
version on each push. See [tests/README.md](tests/README.md).

`dev/` runs InvenTree 1.4.3 in Docker with this plugin installed from the source tree, for
trying the plugin by hand and with a real scanner; see [dev/README.md](dev/README.md).
`dev/usb_update.py` installs an update on a real USB scanner as the browser would, and the
firmware repository's `tools/sync_bridge.py` lets a real USB scanner stand in for a network
one. `dev/check.py` and `dev/check_fleet.py`, which exercise the API against that instance,
came before the test suite and are kept until it covers the browser too.

## How it works

- The **USB connection** (`frontend/src/scanner.ts`) is a service, not part of any panel.
  InvenTree loads a plugin's JavaScript once per page load and keeps it while the user moves
  around the app, so the connection, once made, survives navigation; the panel and the
  dashboard item only attach to it. It opens a scanner silently if the user has granted one
  before, holds it only while the tab is visible and only in one tab at a time (Web Locks),
  drops it when the tab is hidden so the scanner's keyboard output returns for other
  applications, and acts on taps itself: a tag for another bin opens that bin's page, from
  any page in the app. The one limit: nothing in InvenTree's plugin UI runs on every page,
  so after a full page load the connection comes up when the dashboard or a stock location
  page is first shown.
- The **panel** (`frontend/src/Panel.tsx`) appears on a stock location's page and drives
  programming over either route.
- **The API** (`docs/api.md`): the tag's contents for a location, jobs, scanners, and
  `/sync` for scanners. The NDEF message is built in one place, `ndef.py`, byte-for-byte
  the builder in the firmware's `tools/nfcprog.py`.
- **Jobs** (`models.py`) record every programming attempt, USB or network, with its
  outcome. A network job becomes a numbered command that `/sync` hands out until the
  scanner acknowledges it; the scanner's events move the job's state, and `done` links the
  UID to the location on the server.
- **Scanners are machines** (`machine.py`): an *NFC Scanner* type with a *Network scanner*
  driver. InvenTree provides their list, settings and status page; the plugin keeps their
  status, last-seen time and last tap in the machine's shared state. A scheduled task
  marks scanners offline when they stop calling.

## Status

Covered by automated tests (`tests/`) against InvenTree 1.4.3 and 1.5.6: the server side on
SQLite on every push and on PostgreSQL weekly, and the panel, dashboard, USB updates and fleet
page in a headless browser, with a simulated scanner on WebSerial, on every push.

Verified against InvenTree 1.4.3 (local Docker): installation from `plugins.txt`, the
machine type and driver, every endpoint, long polling, a job carried to a real scanner by
`sync_bridge.py`, and the UID landing as the location's barcode. Not yet verified: the
panel and dashboard item rendered in a browser (their bundles build and are served, but
nobody has clicked them), and the WebSerial route from the panel.

Firmware updates (the fleet-updates branch) are verified against the same instance with the
real scanner: a release uploaded and checked, a deployment over the network (downloaded,
restarted, confirmed), and one over USB through `dev/usb_update.py`, which follows the
browser's sequence. Not yet verified: the fleet dashboard item and the update notice in a
browser, and fetching a real release from GitHub. The repository is public and the check
reaches it with no token, but no release has been tagged yet; taking a release is checked
against a stand-in for GitHub.
