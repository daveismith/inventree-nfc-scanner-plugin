# Open issues

What is known to be wrong or missing in this plugin, and not yet fixed. Started from a review
on 2026-10-08 and kept up to date since (last on 2026-10-09, with the browser tests). Line
numbers drift; the function names are the anchor. Several items are the server side of ones
the firmware (`inventree_nfc_scanner`, its own `docs/open-issues.md`) lists too.

Severity: high = loses work or leaves things stuck with no way out; medium = wrong behaviour
in a realistic case; low = rough edge. Fixed items are listed at the end, briefly, for the
record.

## Medium

### 1. Tags protected with an earlier password cannot be reached (deferred past 1.0)


- Where: `views.py` `tag_payload` / `JobListView.post`, `Panel.tsx` `programUsb`: only `pwd`
  and `pack` are sent; the firmware supports `old_pwd`.
- Scenario: an admin changes or clears *Tag password*; every tag protected earlier fails with
  `auth_failed` or `auth_required` on both routes.
- For 1.0 the README says to set the password once, before programming tags. Fix later: keep
  the previous password(s) as a protected setting and send `old_pwd`; the firmware's password
  change is also not tear-safe (its list, item 2).

### 2. The dev instance is reachable from the LAN with documented credentials


- Where: `dev/docker-compose.yml` publishes `8080:8080` on all interfaces; `dev/Caddyfile`
  answers any host on purpose; `dev/README.md` and `dev/.env` document the admin password;
  `dev/add_scanner.py` sets a known password for the scanner user.
- Fix: bind the proxy to `127.0.0.1` by default and make LAN exposure (needed for a real reader)
  an explicit opt-in, documented with its risk.

### 3. Whether a firmware release really works with its `min_plugin` is not tested


- Where: the firmware's `tools/make_release.py` sets `MIN_PLUGIN` by hand; this plugin trusts
  it (`firmware.compatible`) and the plugin's own `PROTO_VERSIONS`.
- Scenario: a firmware change relies on plugin behaviour added in a later release and nobody
  raises `MIN_PLUGIN`; older plugins are offered (and auto-deploy) a firmware they cannot drive.
- Fix: the plan in the firmware repository's `docs/compat-testing-plan.md`: a contract test of
  the firmware (its host simulator) against the plugin at `min_plugin`, in both repositories'
  CI, gating the release.

## Low

### 4. A cancelled job the scanner no longer has waits for the timeout

- Where: `sync.py` `apply_message` (a refused `cancel` never ends a job), `views.py`
  `JobCancelView`; `sync.expire_jobs`.
- Scenario: the scanner restarted unseen, or the job ended and its result was lost; the user
  cancels; the scanner answers `no_job`; the job stays until `expire_jobs` fails it, up to
  its timeout plus two minutes later. A job still `queued` for a scanner that has been
  deactivated is never failed (it can be cancelled by hand, and is cancelled at once then).
- Fix: end the job when its `cancel` is answered `no_job` (and have the firmware answer such a
  cancel `ok`: its list, item 4); fail the queued jobs of a deactivated scanner.

### 5. Holder names reach view-only users


- Where: `sync.py` `_link_barcode` stores "barcode moved from <object>" in `Job.error_detail`,
  which `JobDetailView`/`JobListView` return to anyone with `view_stocklocation` and the panel's
  history renders.
- Fix: store the kind only, or keep the names in the server log alone.

### 6. Tapping the current bin's tag leaves its NFC panel


- Where: `scanner.ts` `goToTaggedLocation` matches `/stock/location/<pk>$`; InvenTree's route
  carries the panel segment (`/web/stock/location/5/nfc-tag`), so the check never matches on
  the panel and navigation resets it. `locationFromUri` also ignores the host, despite its
  comment.
- Fix: match the pk anywhere in the path; compare the URI's host with the server's base URL.

## Noted, not planned

- The tag password is one secret handed to every user with `change_stocklocation` and to every
  scanner token. Operators should treat that permission as "may unlock every tag"; it is
  documented in `docs/api.md`.

- A release whose manifest says `"dev": true` (a development build from the firmware's
  `tools/make_release.py --dev`) is accepted, uploaded or fetched. That is how development
  builds reach a bench scanner; the firmware's release workflow never publishes one.

## Fixed, for the record

- **Fixed with the browser tests:** the browser side has automated tests (`tests/browser/`:
  the panel over USB and the network, the dashboard, updates over USB, the fleet page) against
  every supported InvenTree version. The nightly run against InvenTree's next release failed
  because that release's API tokens are only whole as they are made (the stored key is an
  identifier); the test fixture now takes the token then, and the server suite passes there.
  The fleet models were missing from the Django admin, so
  every plugin registry reload re-imported `admin.py` and stopped part way
  (`AlreadyRegistered`); they are registered now, and a test checks every model is.

- **Fixed with the server test suite:** the server logic has automated tests (`tests/`, run in
  CI against every supported InvenTree version, and weekly against PostgreSQL), including
  `makemigrations --check` (the models now declare their `BigAutoField` ids) and automatic
  deployment in isolation, which the live `dev/check_fleet.py --auto-deploy` could not do
  safely. Concurrent links of one UID could leave it the barcode of several bins (eight of
  eight, in the PostgreSQL test); they now take turns under a per-UID lock.

- **Fixed before 1.0:** answers carry at most two commands; one job at a time per scanner
  (409); events for a job never sent to that scanner are ignored, and a late `done` is believed
  only within 30 minutes and with no newer job for the bin, linked only if its creator may
  still change locations; a message is recorded and applied in one transaction; `boot` is
  required; a base URL that cannot go on a tag is a 400 (InvenTree's own validation already
  prevents one); README and API documentation corrected (the reader id, the integrations, the
  tag password, the 400s, `poll_ms`, `last_tag`).
- **Fixed with fleet updates:** the network update path has a server side (firmware store,
  deployments, `docs/fleet-updates.md`); jobs a restarted scanner forgets end
  (`scanner_restarted`), and so do jobs never reported on (`no_result`).
