# Open issues

Findings from a review of 2026-10-08 that were not fixed at the time, verified against the
code as of commit `0be6ce8`. Line numbers drift; the function names are the anchor. Several
are the server side of items the firmware (`inventree_nfc_scanner`, its own
`docs/open-issues.md`) lists too. They are design-level gaps at the seams, not bugs in any one
function: the plugin trusts the scanner to finish every job it was given.

Severity: high = loses work or leaves things stuck with no way out; medium = wrong behaviour
in a realistic case; low = rough edge.

## High

### 1. A job the scanner loses is never ended (mostly fixed on the fleet-updates branch)

- Fixed: a sync whose `boot` differs from the last one fails the jobs that scanner had taken
  (`scanner_restarted`; `sync.lose_jobs_of_restarted`), and a job taken but never reported on
  by `timeout_s` plus two minutes fails (`no_result`; `sync.expire_jobs`, run every minute).
  Fleet updates needed it: an update waits for the scanner's jobs to end.
- Still open: a `cancel` the scanner answers with `no_job` does not end the job at once (it
  ends at the timeout instead); a job still `queued` for a scanner that has been deactivated
  is never failed (it can be cancelled by hand).

### 2. Answers are unbounded, and a large one jams the reader (fixed for 1.0)

- Fixed: an answer carries at most two commands (`sync.MAX_CMDS_PER_ANSWER`), all the reader
  takes per call; the rest follow. The firmware's 1.0 release names this plugin release as its
  oldest supported plugin for that reason. Checked in `dev/check_rules.py`.

## Medium

### 3. A scanner's token can link chosen UIDs, with the job creator's permissions (fixed for 1.0)

- Fixed: a `done` for a job already given up on (`scanner_offline` or `no_result`) is believed
  only within 30 minutes and only if the location has had no newer job
  (`sync._late_done_believed`); `link_uid` checks that the user who queued the job may still
  change stock locations. A scanner can still report any UID for a job it was really given:
  the server cannot verify which tag was written.

### 4. Several jobs can be queued for one scanner, but the reader runs one (fixed for 1.0)

- Fixed: a job for a scanner with an unfinished one is refused with 409, naming it; the panel
  shows the server's message.

### 5. A message is marked seen before it is applied (fixed for 1.0)

- Fixed: recording a message and applying it are one transaction; a message that raises is
  still recorded (in a savepoint of its own), so one bad message cannot stall the exchange.

### 6. USB job ids collide with plugin job ids (fixed for 1.0)

- Fixed on both sides: the firmware sends a USB job's events to local links only, and the
  plugin ignores events naming a job whose command it never sent to that scanner
  (`sync._was_sent`).

### 7. The plugin cannot reach tags protected with an earlier password (deferred past 1.0)

- Where: `views.py` `tag_payload` / `JobListView.post`, `Panel.tsx` `programUsb`: only `pwd`
  and `pack` are sent; the firmware supports `old_pwd`.
- Scenario: an admin changes or clears *Tag password*; every tag protected earlier fails with
  `auth_failed` or `auth_required` on both routes.
- For 1.0 the README says to set the password once, before programming tags. Fix later: keep
  the previous password(s) as a protected setting and send `old_pwd`; the firmware's password
  change is also not tear-safe (its list, item 6).

### 8. The network update path has no server side (fixed by fleet updates)

- Fixed: firmware is held and served (`firmware.py`, `fleet_views.py`), deployed by admins
  (`fleet.py`), and `ota` answers and events move the scanner's deployment. See
  `docs/fleet-updates.md`.

### 9. The dev instance is reachable from the LAN with documented credentials

- Where: `dev/docker-compose.yml` publishes `8080:8080` on all interfaces; `dev/Caddyfile`
  answers any host on purpose; `dev/README.md` and `dev/.env` document the admin password;
  `dev/check.py` sets a known password for the scanner user.
- Fix: bind the proxy to `127.0.0.1` by default and make LAN exposure (needed for a real reader)
  an explicit opt-in, documented with its risk.

### 10. The README tells admins to read the reader id from the USB serial number (fixed for 1.0)

- Fixed: the README says where the reader id is shown, and that the USB serial does not match.

## Low

### 11. Two concurrent links of one UID can both succeed

- Where: `barcodes.py` `link_uid`: holders are read and then the assignment made with no lock;
  InvenTree's `barcode_hash` has no unique constraint.
- Fix: take a lock (e.g. on the location row and a per-hash advisory lock) around the move.

### 12. Holder names reach view-only users

- Where: `sync.py` `_link_barcode` stores "barcode moved from <object>" in `Job.error_detail`,
  which `JobDetailView`/`JobListView` return to anyone with `view_stocklocation` and the panel's
  history renders.
- Fix: store the kind only, or keep the names in the server log alone.

### 13. Tapping the current bin's tag leaves its NFC panel

- Where: `scanner.ts` `goToTaggedLocation` matches `/stock/location/<pk>$`; InvenTree's route
  carries the panel segment (`/web/stock/location/5/nfc-tag`), so the check never matches on
  the panel and navigation resets it. `locationFromUri` also ignores the host, despite its
  comment.
- Fix: match the pk anywhere in the path; compare the URI's host with the server's base URL.

### 14. A bad base URL gives a 500 (guarded)

- InvenTree's own validation refuses a base URL that is not http(s) or is too long, so this
  cannot happen through its settings; the tag endpoint now answers 400 if it ever does.

### 15. `boot` defaults to 0 when omitted (fixed for 1.0)

- Fixed: a sync without `boot` is refused with 400.

### 16. Documentation that disagrees with the code (fixed for 1.0)

- Fixed: `poll_ms`, `last_tag`, the 400s, the integrations list and "worker" in `docs/api.md`
  and the README.

### 17. No automated tests of the server logic

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

### 18. Whether a firmware release really works with its `min_plugin` is not tested

- Where: the firmware's `tools/make_release.py` sets `MIN_PLUGIN` by hand; this plugin trusts
  it (`firmware.compatible`) and the plugin's own `PROTO_VERSIONS`.
- Scenario: a firmware change relies on plugin behaviour added in a later release and nobody
  raises `MIN_PLUGIN`; older plugins are offered (and auto-deploy) a firmware they cannot drive.
- Fix: the plan in the firmware repository's `docs/compat-testing-plan.md`: a contract test of
  the firmware (its host simulator) against the plugin at `min_plugin`, in both repositories'
  CI, gating the release.

### 19. Automatic deployment cannot be checked in isolation on a live instance

- Where: `dev/check_fleet.py --auto-deploy`. Automatic deployment goes to every scanner older
  than the release, so on a server with real scanners the check gives them a deployment too;
  it withdraws them at once and checks none got past pending, but a scanner calling in during
  that second would be sent the stand-in release (which it refuses: not an image).
- Fix: cover `auto_deploy` with unit tests (see 17) and drop the live variant, or limit
  automatic deployment by a scanner group or machine setting.

## Noted, not planned

- The tag password is one secret handed to every user with `change_stocklocation` and to every
  scanner token. Operators should treat that permission as "may unlock every tag"; it is
  documented in `docs/api.md`.
