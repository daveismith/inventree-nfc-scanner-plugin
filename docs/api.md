# API

Everything the plugin serves is under `/plugin/nfcscanner/`. Two kinds of client use it:

- **The browser**, logged in to InvenTree (session). Only the plugin's own panel needs these,
  but they are plain REST and can be used by anything with a session or an API token.
- **A network scanner**, with an InvenTree API token, which uses one endpoint: `sync/`, and
  fetches firmware updates from `firmware/`.

Plugin URLs accept a session cookie (with CSRF token) or an `Authorization: Token …`
header. HTTP basic authentication is not accepted on plugin URLs, only on `/api/`.

All bodies and responses are JSON. Hex strings are upper case on output and accepted in
either case.

## For the browser

Programming a tag needs the user to have change permission on stock locations, because
the tag's UID becomes the location's barcode. Reading jobs and scanners needs view
permission on stock locations. A user with neither gets 403 from every endpoint below.

**The tag password.** When the *Tag password* setting is set, every user who may program
tags receives it, in `api/location/<pk>/tag/` and in the commands sent to network
scanners, because their scanner needs it to write the tag. It is one secret shared by every
tag the server programs. Anyone allowed to program tags can therefore read it; the
permission should be given with that in mind. The plugin keeps it out of job records and
scanner state, drops it from a command's stored payload once the scanner has acknowledged
the command, and masks it in the admin.

### `GET api/location/<pk>/tag/`

What a `program` job for this location carries. This is the one place the tag's contents
are built, so the USB route fetches it rather than building its own.

```json
{
  "location": 42,
  "text": "INV-SL42",
  "uri": "https://inventree.example/web/stock/location/42",
  "ndef": "9101...",
  "timeout_s": 60,
  "pwd": "A1B2C3D4",
  "pack": "1234"
}
```

`pwd` and `pack` are present only when the *Tag password* setting is set. `ndef` is the
complete NDEF message as hex: a URI record for the location's page (built from the
*Base URL* global setting, which must be set) and a Text record with InvenTree's short
barcode for the location. 400 with `{"base_url": "..."}` when the base URL is not set.

### `POST api/location/<pk>/link/`

Make a tag's UID the location's barcode: `{"uid": "04A1B2C3D4E5F6"}` (an NTAG21x UID: exactly
14 hex digits, so that no product barcode can be passed off as one).
Unlike InvenTree's own `/api/barcode/link/`, which refuses a barcode something already
carries, this takes it from whatever held it before, which is what re-programming a tag for
another bin means. The user must have change permission on each such holder's model (a
Part, a StockItem, another location); otherwise 403 and nothing changes. The move is one
transaction and is written to the server log. Answers
`{"location": 42, "uid": "…", "outcome": "linked" | "already linked" | "moved from <what>"}`;
400 for a malformed UID or a link InvenTree refuses. The network route does the same on the
server when a job reports `done`, on behalf of the user who queued the job.

### `POST api/location/<pk>/jobs/usb/`

Record the outcome of a job the browser did itself over USB, so the history is complete.

```json
{"state": "done", "uid": "04A1B2C3D4E5F6", "tag_type": "ntag215", "protected": true}
```

`state` is `done`, `failed` or `cancelled`; `kind` (`program` or `wipe`), `overwrite`,
`error` and `error_detail` are optional. Answers 201 with the job.

### `GET api/scanners/`

The network scanners (InvenTree machines of type *NFC Scanner*), with their state.

```json
[{
  "id": "79b50fa8-11cd-4430-b239-8069c9e56f06",
  "name": "Desk scanner",
  "driver": "nfc-network",
  "status": "online",
  "status_text": "last seen 2026-10-06 02:11:51 UTC",
  "online": true,
  "last_seen": "2026-10-06T02:11:51.311747+00:00",
  "last_tag": {"uid": "04AABBCCDDEEFF", "type": "ntag215", "text": "INV-SL1", "uri": null, "protected": true, "error": null, "at": "..."},
  "location": null,
  "warning": null
}]
```

`status` is one of `online`, `busy` (a job is waiting or writing), `offline`, `unknown`,
`error`. `last_tag` is the last tap reported outside a job, or null. A `warning` (null when
there is none) says when two scanners are configured with the same user, since one token
then serves both; give each scanner a user of its own.

### `POST api/jobs/`

Queue a job for a network scanner.

```json
{"location": 42, "scanner": "79b50fa8-…", "overwrite": false, "kind": "program"}
```

`kind` is `program` (default) or `wipe`. Answers 201 with the job. 400 when the scanner is
unknown, inactive or not initialised, not a network scanner, or (with the shared cache, where
its status is known) offline.

### `GET api/jobs/?location=<pk>&scanner=<id>` and `GET api/jobs/<id>/`

Jobs, newest first (at most 50), or one job:

```json
{
  "id": 317, "kind": "program", "location": 42,
  "scanner": {"id": "79b50fa8-…", "name": "Desk scanner"},
  "created_by": 1, "created_by_name": "admin",
  "created_at": "…", "updated_at": "…", "finished_at": null,
  "state": "waiting", "overwrite": false, "timeout_s": 60,
  "uid": "", "tag_type": "", "protected": null,
  "error": "", "error_detail": "", "existing_text": "", "existing_uri": ""
}
```

`state` moves through `queued` (not yet collected by the scanner), `sent`, `waiting` (for a
tag), `writing`, and ends in `done`, `failed` or `cancelled`. A `scanner` of null means a
USB job. On `failed`, `error` is the scanner's error code; for `not_blank`, `existing_text`
and `existing_uri` say what the tag already holds, so the user can be asked before
overwriting. A job whose scanner stops answering fails with `scanner_offline`, whether it
had been collected or was still queued; a job cannot be queued for a scanner that is
offline already. A job the scanner had taken fails with `scanner_restarted` when the scanner
calls in after a restart (it has forgotten the job), and with `no_result` when nothing has been
heard of it two minutes after its own timeout. A job cannot be queued for a scanner that is
installing a firmware update (400).

### `POST api/jobs/<id>/cancel/`

A job the scanner has not collected is cancelled at once, as is any job of a scanner that
has been deactivated or deleted. One a live scanner has collected gets a `cancel` command,
and ends `cancelled` when the scanner confirms. Answers with the job. Repeating it does not
queue a second `cancel`.

## For a scanner

### `POST sync/`

The scanner's one call: what has happened since last time, and whatever the server has for
it. The scanner is identified by `reader`, which must match the *Reader ID* setting of an
active *NFC Scanner* machine with the *Network scanner* driver, and the token must belong
to the user in that machine's *User* setting.

Request:

```json
{
  "reader": "nfc-34b7da52a084",
  "fw": "0.2.0",
  "boot": 17,
  "proto": 1,
  "ack": 41,
  "wait_s": 25,
  "msgs": [
    {"seq": 12, "evt": "tag", "uid": "04A1B2C3D4E5F6", "type": "ntag215", "text": "INV-SL4", "protected": true},
    {"seq": 13, "rsp": "program", "ok": true, "id": 317}
  ]
}
```

| Field | |
| --- | --- |
| `reader` | The scanner's id, from its MAC address |
| `fw` | Optional: the firmware version it runs, for the fleet page |
| `boot` | A number that changes whenever the scanner restarts; its message numbering starts again with it |
| `proto` | Protocol version; this plugin speaks 1 |
| `ack` | The highest command `seq` the scanner has acted on |
| `wait_s` | How long the server may hold this request waiting for a command; 0 answers at once |
| `msgs` | `rsp` and `evt` objects exactly as the scanner emits them over USB, each with a `seq` |

Response (200):

```json
{
  "ack": 13,
  "cmds": [
    {"seq": 42, "cmd": "program", "id": 317, "ndef": "9101…", "pwd": "A1B2C3D4", "pack": "1234", "timeout_ms": 60000}
  ],
  "poll_ms": 1000
}
```

| Field | |
| --- | --- |
| `ack` | The highest message `seq` the server has stored from this call |
| `cmds` | Commands exactly as the scanner takes them over USB, each with a `seq`, oldest first |
| `poll_ms` | Optional: how soon to call again when idle |

Rules:

- A command stays in `cmds` on every call until the scanner's `ack` covers its `seq`. The
  scanner ignores a `seq` it has already acted on.
- A message is applied once: a repeat of the same `(reader, boot, seq)` is acknowledged
  and ignored.
- A `program` or `wipe` command's `id` is the job's id; the scanner's `rsp` and events
  carry it back, and move the job's state.
- A `tag` event outside a job is kept as the scanner's last tap.
- With the *Long polling* setting on, a call with nothing to deliver is held, up to
  `wait_s` or the *Longest hold* setting (at most 60 s), whichever is less, and answered as
  soon as a command is queued. Off, every call is answered at once; a scanner should then
  wait at least a second between idle calls. Each held call occupies a server worker for
  its duration, so hold only as many scanners as the server has workers to spare.

Errors: 401 for a bad token, 403 for a `reader` that no active machine is configured with
or that is not this token's (the two are not told apart, so a token cannot be used to find
out which reader ids exist), 400 for a malformed body (`boot`, `ack`, `wait_s` not whole
numbers from 0 to 2^31-1, or `msgs` not a list of objects). A reader id that two active
machines share is treated as unknown until that is fixed.

Every value in a message is bounded and typed before it is stored: strings are cut to the
field's length, a `uid` that is not 14 hex digits is ignored, and so on. A negative answer
(`ok: false`) ends a job only when it answers that job's `program` or `wipe`; a refused
`cancel` does not, since the job's `done` follows. The
password and PACK in a `program` or `wipe` command are removed from the plugin's record of
the command once the scanner acknowledges it, or the job ends. Acknowledged commands and seen
messages are deleted after two days; the numbering is kept apart and only ever goes up. A
`done` that arrives after the job was failed as `scanner_offline` is still applied: the tag
was written, so the record and the barcode follow.

A scanner in the middle of a firmware update sends `rsp` to `ota` and `ota` events (`state`:
`downloading`, `restarting` or `failed`, with `error` and `detail`); they move its deployment
(see below), not a job.

The reference client is `tools/sync_bridge.py` in the firmware repository, which drives a
USB scanner through this exchange.

## Firmware updates

How the pieces fit is in [fleet-updates.md](fleet-updates.md). An admin here is a superuser,
or a user whose group has change permission on InvenTree's *Admin* role (the permission
`machine.change_machineconfig`). Everyone else gets 403 from `api/fleet/`.

### For admins

#### `GET api/fleet/`

```json
{
  "scanners": [{
    "reader": "nfc-34b7da52a084", "fw": "0.2.0", "proto": 1,
    "last_seen": "…", "last_via": "network", "last_user": null,
    "machine": {"id": "79b50fa8-…", "name": "Desk scanner"},
    "outdated": false,
    "deployment": null
  }],
  "firmware": [{
    "id": 3, "version": "0.2.0", "prerelease": false, "source": "github",
    "release_url": "https://github.com/…/releases/tag/v0.2.0", "published_at": "…",
    "added_at": "…", "available": true, "size": 1202240, "sha256": "…", "proto": 1,
    "settings_version": 1, "min_plugin": "0.1.0", "git_sha": "…", "merged": true,
    "incompatible": null
  }],
  "newest": "0.2.0",
  "last_check": {"at": "…", "added": [], "errors": [], "pruned": []},
  "repo": "daveismith/inventree_nfc_scanner",
  "policy": "deferrable"
}
```

Every scanner the server has heard of, over the network (each `/sync`) or USB (a browser's
check-in), with its unfinished deployment if it has one. `outdated` is true when a newer
release than the one it runs is held. `incompatible` says why a release cannot be deployed
(another protocol, a newer plugin needed, or its image pruned), or is null.

#### `POST api/fleet/check/`

Look for new releases now (otherwise every *Check for firmware every* hours). Answers
`{"added": ["0.2.1"], "errors": [], "pruned": [], "at": "…"}`. A release is taken only when
each file's sha256 matches the manifest and GitHub's own digest of the asset.

#### `POST api/fleet/upload/`

Multipart, for a server without internet access: `manifest` (the release's manifest.json),
`app` (its app image) and optionally `merged`. 201 with the release; 400 when a file does not
match the manifest, the manifest is not for this scanner, or the version is held already
with a different image.

#### `DELETE api/fleet/firmware/<id>/`

Delete a release's images; its record goes too unless a deployment refers to it. 400 while a
deployment of it is unfinished.

#### `POST api/fleet/deploy/`

```json
{"firmware": 3, "scanners": ["nfc-34b7da52a084"], "required": null,
 "required_after": null, "allow_downgrade": false}
```

`scanners` is a list of reader ids, or `"all"`. `required`: whether the user at a USB scanner
may put it off (null: the *USB update policy* setting); `required_after`: from when it may not.
Answers `{"results": [{"reader": "…", "deployment": 12} | {"reader": "…", "refused": "why"}]}`.
Refused for a scanner: one already running that version, one mid-update, an older version
without `allow_downgrade`, and (always) a network scanner going below the network-settings
layout it keeps. A deployment still pending for the same scanner is superseded. 400 for an
incompatible release.

#### `GET api/fleet/deployments/?scanner=<reader id>`

The last 100 deployments, newest first:

```json
[{"id": 12, "reader": "nfc-…", "version": "0.2.0", "state": "confirmed", "via": "network",
  "from_version": "0.1.0", "required": false, "required_flag": null, "required_after": null,
  "requested_by": "admin", "requested_at": "…", "started_at": "…", "finished_at": "…",
  "attempts": 1, "deferrals": 0, "error": "", "detail": ""}]
```

`state`: `pending` (waiting for the scanner), `sent`, `downloading`, `restarting`, then
`confirmed`, `failed`, `rolled_back` (it came back on the old version), `superseded` or
`cancelled`. An attempt cut short before the restart (the scanner was busy, lost power, the
browser closed) goes back to `pending`, up to three attempts.

#### `POST api/fleet/deployments/<id>/cancel/`

Withdraw a pending deployment. 400 once the scanner has started on it.

#### `DELETE api/fleet/scanners/<reader id>/`

Forget a scanner and its deployment history (one taken out of service). It reappears when
it is next heard from. 400 mid-update.

### For the browser with a USB scanner

These need the tag programming permission, and answer only about the scanner named.

- `POST api/usb/checkin/` `{"reader", "fw", "proto"}`: the browser has connected to a scanner.
  Answers `{"reader", "update": {"id", "version", "required", "required_after", "size",
  "sha256", "url", "deferrals"} | null, "last": {"id", "version", "state", "error",
  "detail"} | null}`; `last` is the latest USB update's outcome, so the browser can say how
  the one it installed went.
- `POST api/usb/deployments/<id>/start/` `{"reader"}`: claim it to install it now.
- `POST api/usb/deployments/<id>/defer/` `{"reader"}`: "Later". 400 if it is required.
- `POST api/usb/deployments/<id>/report/` `{"reader", "state", "error", "detail"}`: `state`
  is `downloading`, `restarting` or `failed`.

### Images

`GET firmware/<version>/<file>`: a release's app or merged image, to any signed-in user or
API token (the scanner fetches with its own). `ETag` is its sha256.
