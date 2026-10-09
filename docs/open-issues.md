# Open issues

What is known to be wrong or missing in this plugin, and not yet fixed. Started from a review
on 2026-10-08 and kept up to date since (last on 2026-10-08, with the fixes before 1.0). Line
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
  `dev/check.py` sets a known password for the scanner user.
- Fix: bind the proxy to `127.0.0.1` by default and make LAN exposure (needed for a real reader)
  an explicit opt-in, documented with its risk.

### 3. No automated tests of the server logic


- `dev/check.py` needs a live instance and is not run in CI; there is no Django test suite and
  no `makemigrations --check`. The locking, the counter, retirement and the stale task have no
  automated coverage.
- Fix: a minimal Django test suite (sync, cancel, stale marking, link) run in CI against
  SQLite, plus `makemigrations --check`.
- `dev/check_rules.py` checks a few rules that need database state (the late-`done` window,
  the permission re-check, the answer cap) inside the dev server, in a rolled-back
  transaction: a start on what a test suite would cover.
- Fleet updates added `dev/check_fleet.py` (also live-instance only). The deployment state
  machine (`fleet.py`: `_settle`, `apply_ota_message`, `check_deployments`, `auto_deploy`) is
  the first thing worth unit tests, since its timing cases (timeouts, retries, a restart mid
  download) are slow to reach live.
- Caveat for `makemigrations --check`: InvenTree's default id type differs from the one
  migrations 0001 and 0003 use (`BigAutoField`), so `makemigrations` proposes altering the
  existing ids. Set the ids explicitly on the models (or find where InvenTree's app config for
  plugins takes `default_auto_field`) before adding the check.

### 4. Whether a firmware release really works with its `min_plugin` is not tested


- Where: the firmware's `tools/make_release.py` sets `MIN_PLUGIN` by hand; this plugin trusts
  it (`firmware.compatible`) and the plugin's own `PROTO_VERSIONS`.
- Scenario: a firmware change relies on plugin behaviour added in a later release and nobody
  raises `MIN_PLUGIN`; older plugins are offered (and auto-deploy) a firmware they cannot drive.
- Fix: the plan in the firmware repository's `docs/compat-testing-plan.md`: a contract test of
  the firmware (its host simulator) against the plugin at `min_plugin`, in both repositories'
  CI, gating the release.

## Low

### 5. A cancelled job the scanner no longer has waits for the timeout

- Where: `sync.py` `apply_message` (a refused `cancel` never ends a job), `views.py`
  `JobCancelView`; `sync.expire_jobs`.
- Scenario: the scanner restarted unseen, or the job ended and its result was lost; the user
  cancels; the scanner answers `no_job`; the job stays until `expire_jobs` fails it, up to
  its timeout plus two minutes later. A job still `queued` for a scanner that has been
  deactivated is never failed (it can be cancelled by hand, and is cancelled at once then).
- Fix: end the job when its `cancel` is answered `no_job` (and have the firmware answer such a
  cancel `ok`: its list, item 4); fail the queued jobs of a deactivated scanner.

### 6. Two concurrent links of one UID can both succeed


- Where: `barcodes.py` `link_uid`: holders are read and then the assignment made with no lock;
  InvenTree's `barcode_hash` has no unique constraint.
- Fix: take a lock (e.g. on the location row and a per-hash advisory lock) around the move.

### 7. Holder names reach view-only users


- Where: `sync.py` `_link_barcode` stores "barcode moved from <object>" in `Job.error_detail`,
  which `JobDetailView`/`JobListView` return to anyone with `view_stocklocation` and the panel's
  history renders.
- Fix: store the kind only, or keep the names in the server log alone.

### 8. Tapping the current bin's tag leaves its NFC panel


- Where: `scanner.ts` `goToTaggedLocation` matches `/stock/location/<pk>$`; InvenTree's route
  carries the panel segment (`/web/stock/location/5/nfc-tag`), so the check never matches on
  the panel and navigation resets it. `locationFromUri` also ignores the host, despite its
  comment.
- Fix: match the pk anywhere in the path; compare the URI's host with the server's base URL.

### 9. Automatic deployment cannot be checked in isolation on a live instance


- Where: `dev/check_fleet.py --auto-deploy`. Automatic deployment goes to every scanner older
  than the release, so on a server with real scanners the check gives them a deployment too;
  it withdraws them at once and checks none got past pending, but a scanner calling in during
  that second would be sent the stand-in release (which it refuses: not an image).
- Fix: cover `auto_deploy` with unit tests (see 3) and drop the live variant, or limit
  automatic deployment by a scanner group or machine setting.

## Noted, not planned

- The tag password is one secret handed to every user with `change_stocklocation` and to every
  scanner token. Operators should treat that permission as "may unlock every tag"; it is
  documented in `docs/api.md`.

## Fixed, for the record

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
