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

### 2. Answers are unbounded, and a large one jams the reader (shared with the firmware)

- Where: `sync.py` `handle_sync` step 3 sends every pending command; the reader reads 8 KB.
- Scenario: around 30 queued jobs for one reader. Each cancel adds a `cancel` command and makes
  it worse. The reader keeps calling, so it is never marked offline; only deactivating the
  machine clears it.
- Fix: cap `cmds` per answer (the reader takes two per call); refuse or queue-limit new jobs for
  a scanner with many pending; see also 4.

## Medium

### 3. A scanner's token can link chosen UIDs, with the job creator's permissions

- Where: `sync.py` `apply_message` (`done` takes `uid` from the message; the late-`done`
  exception for jobs failed as `scanner_offline` has no age limit), `barcodes.py` `link_uid`
  (checks only that the actor is active, not that they still hold `change_stocklocation`).
- Scenario: a leaked scanner token (its user is meant to hold no permissions) waits for or
  forces `scanner_offline` failures, then sends `done` messages with chosen UIDs and moves other
  objects' barcodes as the admin who queued those jobs. An hours-late `done` can also pull a UID
  back to an old bin after the tag was re-programmed elsewhere.
- Fix: bound the late-`done` path (an age limit; only if no newer job or link exists for that
  location); re-check the creator's `change_stocklocation` at link time; consider whether a
  scanner may choose the UID at all (the reader could report the UID of the tag it was told to
  write, but the server cannot verify it).

### 4. Several jobs can be queued for one scanner, but the reader runs one

- Where: `views.py` `JobListView.post` (no check for an unfinished job on that scanner);
  `sync.py` turns the reader's `busy` refusal into a failed job.
- Scenario: two users or two tabs queue jobs for the same scanner; every job after the first
  fails with `busy` within a second instead of waiting its turn.
- Fix: refuse (409) or queue server-side, sending the next command only when the previous job
  has ended.

### 5. A message is marked seen before it is applied

- Where: `sync.py` `handle_sync` step 2: `ScannerMessage.get_or_create`, then `apply_message`,
  not in one transaction.
- Scenario: a transient database error or a process death between the two steps; the scanner's
  resend is then treated as a duplicate and dropped. A lost `done` means the barcode is never
  linked and the job stays in `writing` (compounds 1).
- Fix: apply each message inside the transaction that records it, and roll both back together.

### 6. USB job ids collide with plugin job ids (shared with the firmware)

- Where: `sync.py` `apply_message` looks a job up by `id` within the machine; the reader
  forwards events of USB-started jobs too (`nfcprog.py` defaults to `--id 1`; the panel uses
  `Date.now() % 1000000`).
- Scenario: a USB `done` with id 1 matches plugin job 1 (in particular one re-opened by the
  late-`done` path) and links the new tag's UID to that job's location.
- Fix: the reader should forward only events of jobs the server started (firmware side), and
  the server should ignore events whose job it did not send a command for recently (e.g. require
  an unacknowledged or recently retired command for that job id).

### 7. The plugin cannot reach tags protected with an earlier password

- Where: `views.py` `tag_payload` / `JobListView.post`, `Panel.tsx` `programUsb`: only `pwd`
  and `pack` are sent; the firmware supports `old_pwd`.
- Scenario: an admin changes or clears *Tag password*; every tag protected earlier fails with
  `auth_failed` or `auth_required` on both routes, with no way to recover and no warning in the
  README.
- Fix: keep the previous password(s) as a protected setting and send `old_pwd`; warn in the
  settings description and README.

### 8. The network update path has no server side

- Where: `core.py` has no firmware setting or endpoint; `sync.py` drops `ota` events (they carry
  no `id`), so update progress is invisible on the server. The firmware accepts a network `ota`
  only for an image on the plugin's own origin.
- Fix: an endpoint that holds uploaded firmware and serves it to a scanner's token; a way to
  queue an `ota` command (admin action or API) with the image's sha256; store `ota` events on the
  machine's state.

### 9. The dev instance is reachable from the LAN with documented credentials

- Where: `dev/docker-compose.yml` publishes `8080:8080` on all interfaces; `dev/Caddyfile`
  answers any host on purpose; `dev/README.md` and `dev/.env` document the admin password;
  `dev/check.py` sets a known password for the scanner user.
- Fix: bind the proxy to `127.0.0.1` by default and make LAN exposure (needed for a real reader)
  an explicit opt-in, documented with its risk.

### 10. The README tells admins to read the reader id from the USB serial number

- Where: `README.md` (machine setup). The USB serial is the upper-case MAC with no prefix
  (`34B7DA52A084`); the reader id the firmware presents is `nfc-34b7da52a084`, matched exactly.
- Fix: say to take the id from the reader's `info` or `net` answer (`nfcprog.py net`), or
  normalise on the server (lower-case, add the prefix).

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

### 14. A bad base URL gives a 500

- Where: `ndef.py` `build_message` raises `ValueError` for a non-http(s) scheme or an overlong
  URI; `views.py` catches only a missing base URL.
- Fix: catch and answer 400 with the reason.

### 15. `boot` defaults to 0 when omitted

- Where: `sync.py` `handle_sync`. A client that omits it reuses `(0, seq)` across restarts and
  its post-restart messages are dropped as duplicates for two days.
- Fix: require `boot` (400 when absent).

### 16. Documentation that disagrees with the code

- `docs/api.md` documents `poll_ms`, which `handle_sync` never returns.
- `docs/api.md` says `last_tag` is a tap "outside a job"; `apply_message` records every `tag`
  event.
- `docs/api.md`'s list of 400s omits `proto != 1`.
- `README.md` says to enable *event integration*; the plugin uses no EventMixin.
- `README.md` says a held call occupies a web worker; with gunicorn's threads it occupies a
  thread.

### 17. No automated tests of the server logic

- `dev/check.py` needs a live instance and is not run in CI; there is no Django test suite and
  no `makemigrations --check`. The locking, the counter, retirement and the stale task have no
  automated coverage.
- Fix: a minimal Django test suite (sync, cancel, stale marking, link) run in CI against
  SQLite, plus `makemigrations --check`.

## Noted, not planned

- The tag password is one secret handed to every user with `change_stocklocation` and to every
  scanner token. Operators should treat that permission as "may unlock every tag"; it is
  documented in `docs/api.md`.
