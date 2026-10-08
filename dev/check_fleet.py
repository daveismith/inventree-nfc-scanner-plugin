#!/usr/bin/env python3
"""Checks of fleet firmware updates against the local InvenTree (dev/README.md).

Plays a scanner of its own (reader nfc-f1ee7c0ffee0, machine "Fleet check scanner", user
scanner-fleet), so a real scanner on the same instance can stay on the air. Covers: the
registry, uploading and validating releases, fetching them from a stand-in for GitHub's API,
deploying (and what is refused), the network route through /sync step by step (including a
busy scanner, an interrupted download, a rollback and a scanner that restarts mid-job), the
USB route's check-in, deferral, start and report, the image endpoint, and who may do what.

    python3 check_fleet.py [--base http://127.0.0.1:8080] [--host inventree.localhost:8080]

The stand-in for GitHub listens on this machine; the server reaches it as
host.docker.internal (Docker Desktop). Everything it creates is removed at the end.
"""

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check import HTTP_PORT, Api, check  # noqa: E402
import check as base  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
READER = "nfc-f1ee7c0ffee0"
MACHINE = "Fleet check scanner"
USER, PASSWORD = "scanner-fleet", "scanner-fleet-dev"
P = "/plugin/nfcscanner"
FAKE_PORT = 8769


def release(version, size=4096, proto=1, settings_version=1, min_plugin="0.1.0"):
    """A made-up release: (manifest, app bytes, merged bytes)."""
    app = secrets.token_bytes(size)
    merged = b"\xff" * 64 + app
    name = f"inventree_nfc_scanner-{version}"
    manifest = {
        "name": "inventree_nfc_scanner",
        "version": version,
        "target": "esp32s3",
        "idf": "v6.1",
        "proto": proto,
        "settings_version": settings_version,
        "min_plugin": min_plugin,
        "git_sha": "0" * 40,
        "app": {
            "file": f"{name}.bin",
            "size": len(app),
            "sha256": hashlib.sha256(app).hexdigest(),
            "offset": 0x20000,
        },
        "merged": {
            "file": f"{name}-merged.bin",
            "size": len(merged),
            "sha256": hashlib.sha256(merged).hexdigest(),
            "offset": 0,
        },
    }
    return manifest, app, merged


def multipart(api, path, token, files):
    """POST files as multipart/form-data."""
    import urllib.error
    import urllib.request

    boundary = uuid.uuid4().hex
    body = b""
    for field, (filename, data) in files.items():
        body += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        api.base + path,
        method="POST",
        data=body,
        headers={
            "Host": api.host,
            "Authorization": f"Token {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:300]


def upload(api, token, rel, app=None):
    manifest, good_app, merged = rel
    return multipart(
        api,
        f"{P}/api/fleet/upload/",
        token,
        {
            "manifest": ("manifest.json", json.dumps(manifest).encode()),
            "app": (manifest["app"]["file"], good_app if app is None else app),
            "merged": (manifest["merged"]["file"], merged),
        },
    )


def raw_get(api, path, token=None):
    import urllib.error
    import urllib.request

    headers = {"Host": api.host}
    if token:
        headers["Authorization"] = f"Token {token}"
    req = urllib.request.Request(api.base + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class FakeGitHub:
    """Just enough of GitHub's releases API: a list, and assets by API URL."""

    def __init__(self, port):
        self.releases = []
        self.assets = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/repos/check/firmware/releases"):
                    data = json.dumps(fake.releases).encode()
                    ctype = "application/json"
                elif self.path.startswith("/assets/") and self.path[8:] in fake.assets:
                    if self.headers.get("Accept") != "application/octet-stream":
                        self.send_response(415)
                        self.end_headers()
                        return
                    data = fake.assets[self.path[8:]]
                    ctype = "application/octet-stream"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, rel, *, prerelease=False, bad_digest=False, base_url):
        manifest, app, merged = rel
        assets = []
        for name, data in (
            ("manifest.json", json.dumps(manifest).encode()),
            (manifest["app"]["file"], app),
            (manifest["merged"]["file"], merged),
        ):
            key = f"{manifest['version']}-{name}"
            self.assets[key] = data
            digest = hashlib.sha256(data).hexdigest()
            if bad_digest and name.endswith(".bin") and "merged" not in name:
                digest = "0" * 64
            assets.append({
                "name": name,
                "size": len(data),
                "url": f"{base_url}/assets/{key}",
                "digest": f"sha256:{digest}",
            })
        self.releases.insert(
            0,
            {
                "tag_name": f"v{manifest['version']}",
                "draft": False,
                "prerelease": prerelease,
                "html_url": f"https://github.com/check/firmware/releases/tag/v{manifest['version']}",
                "published_at": "2026-10-08T00:00:00Z",
                "assets": assets,
            },
        )


def prepare(api, admin):
    """A user, token and network machine for the check's own scanner. Idempotent."""
    st, users = api.call("GET", f"/api/user/?search={USER}", token=admin)
    user = next((u for u in users if u["username"] == USER), None)
    if user is None:
        st, user = api.call(
            "POST",
            "/api/user/",
            token=admin,
            body={
                "username": USER,
                "first_name": "Fleet",
                "last_name": "Check",
                "email": "fleet@example.com",
                "password": PASSWORD,
            },
        )
        assert st == 201, user
    api.call(
        "PUT",
        f"/api/user/{user['pk']}/set-password/",
        token=admin,
        body={"password": PASSWORD, "override_warning": True},
    )
    st, tok = api.call(
        "GET", "/api/user/token/?name=nfc-fleet", basic=f"{USER}:{PASSWORD}"
    )
    assert st == 200 and "token" in tok, tok
    st, machines = api.call("GET", "/api/machine/", token=admin)
    machine = next((m for m in machines if m["name"] == MACHINE), None)
    if machine is None:
        st, machine = api.call(
            "POST",
            "/api/machine/",
            token=admin,
            body={
                "name": MACHINE,
                "machine_type": "nfc-scanner",
                "driver": "nfc-network",
                "active": True,
            },
        )
        assert st == 201, machine
    mid = machine["pk"]
    # Left inactive by the last run, so it does not sit on the dashboard as an offline scanner.
    api.call("PATCH", f"/api/machine/{mid}/", token=admin, body={"active": True})
    api.call(
        "PUT",
        f"/api/machine/{mid}/settings/D/READER_ID/",
        token=admin,
        body={"value": READER},
    )
    api.call(
        "PUT",
        f"/api/machine/{mid}/settings/D/USER/",
        token=admin,
        body={"value": str(user["pk"])},
    )
    api.call("POST", f"/api/machine/{mid}/restart/", token=admin)
    time.sleep(2)
    st, locations = api.call("GET", "/api/stock/location/?search=Bin%20A1", token=admin)
    loc = next(x for x in locations if x["name"] == "Bin A1")
    return tok["token"], mid, loc["pk"]


def set_setting(api, admin, key, value):
    st, body = api.call(
        "PATCH",
        f"/api/plugins/nfcscanner/settings/{key}/",
        token=admin,
        body={"value": value},
    )
    assert st == 200, (key, st, body)


def get_setting(api, admin, key):
    st, body = api.call("GET", f"/api/plugins/nfcscanner/settings/{key}/", token=admin)
    return body.get("value") if st == 200 else None


def run(api, admin, scanner, loc):
    # Versions of this run's own: the scanner's starting firmware, and the releases it makes
    # (90.<stamp>.N), so that nothing a real server holds is touched or collided with.
    stamp = int(time.time())
    OLD = f"89.{stamp}.0"
    state = {"boot": int(time.time()) % 100000, "ack": 0, "seq": 0, "fw": OLD}

    def sync(msgs=(), **extra):
        body = {
            "reader": READER,
            "fw": state["fw"],
            "boot": state["boot"],
            "proto": 1,
            "ack": state["ack"],
            "wait_s": 0,
            "msgs": list(msgs),
        }
        body.update(extra)
        st, out = api.call("POST", f"{P}/sync/", token=scanner, body=body)
        assert st == 200, (st, out)
        return out

    def say(**msg):
        state["seq"] += 1
        return sync([dict(msg, seq=state["seq"])])

    def restart(fw):
        state.update(boot=state["boot"] + 1, ack=0, seq=0, fw=fw)
        return sync()

    def deployments():
        return api.call(
            "GET", f"{P}/api/fleet/deployments/?scanner={READER}", token=admin
        )[1]

    def fleet_scanner():
        st, body = api.call("GET", f"{P}/api/fleet/", token=admin)
        return next((s for s in body["scanners"] if s["reader"] == READER), None)

    def deploy(fw_id, **kw):
        return api.call(
            "POST",
            f"{P}/api/fleet/deploy/",
            token=admin,
            body={"firmware": fw_id, "scanners": [READER], **kw},
        )

    def ota_cmds(out):
        return [c for c in out["cmds"] if c.get("cmd") == "ota"]

    # Start from nothing: forget this scanner and anything an earlier run left.
    api.call("DELETE", f"{P}/api/fleet/scanners/{READER}/", token=admin)
    sync(ack=10**6)
    state["ack"] = 10**6
    sync()
    state["ack"] = 0
    restart(OLD)

    # --- who may do what
    st, _ = api.call("GET", f"{P}/api/fleet/", token=scanner)
    check(st == 403, "the fleet page is for admins only", st)
    st, _ = api.call("POST", f"{P}/api/fleet/check/", token=scanner)
    check(st == 403, "so is looking for releases", st)
    st, _ = api.call(
        "POST",
        f"{P}/api/usb/checkin/",
        token=scanner,
        body={"reader": READER, "fw": OLD},
    )
    check(
        st == 403, "a user who may not program tags cannot check in a USB scanner", st
    )

    # --- the registry
    sc = fleet_scanner()
    check(
        sc is not None
        and sc["fw"] == OLD
        and sc["last_via"] == "network"
        and sc["machine"]["name"] == MACHINE,
        "a sync puts the scanner in the registry, with its version and machine",
        sc,
    )

    # --- releases: upload and its checks
    v = lambda minor, suffix="": f"90.{stamp}.{minor}{suffix}"  # noqa: E731
    r1, r2, r_old = release(v(1)), release(v(2)), release(OLD)
    st, body = multipart(
        api, f"{P}/api/fleet/upload/", admin, {"manifest": ("manifest.json", b"{}")}
    )
    check(st == 400, "an upload without the app image is refused", (st, body))
    st, body = upload(api, admin, r1, app=r1[1][:-1] + bytes([r1[1][-1] ^ 1]))
    check(
        st == 400 and "does not match" in str(body),
        "an image that does not match its manifest is refused",
        (st, body),
    )
    bad = json.loads(json.dumps(r1[0]))
    bad["target"] = "esp32c3"
    st, body = upload(api, admin, (bad, r1[1], r1[2]))
    check(
        st == 400 and "esp32c3" in str(body),
        "a release for another chip is refused",
        (st, body),
    )
    st, fw1 = upload(api, admin, r1)
    check(
        st == 201 and fw1["version"] == v(1) and fw1["incompatible"] is None,
        "a genuine release is taken",
        (st, fw1),
    )
    st, again = upload(api, admin, r1)
    check(
        st == 201 and again["id"] == fw1["id"],
        "uploading it again is harmless",
        (st, again),
    )
    other = release(v(1))
    st, body = upload(api, admin, other)
    check(
        st == 400 and "different image" in str(body),
        "a different image under a version already held is refused",
        (st, body),
    )
    st, fw2 = upload(api, admin, r2)
    st, fw_old = upload(api, admin, r_old)
    st, fw_proto = upload(api, admin, release(v(3), proto=2))
    check(
        st == 201 and fw_proto["incompatible"],
        "a release for another protocol is held but marked incompatible",
        fw_proto,
    )
    st, fw_future = upload(api, admin, release(v(4), min_plugin="99.0.0"))
    check(
        fw_future["incompatible"] and "99.0.0" in fw_future["incompatible"],
        "so is one that needs a newer plugin",
        fw_future,
    )

    # --- the image endpoint
    path = f"{P}/firmware/{v(1)}/{r1[0]['app']['file']}"
    st, _ = raw_get(api, path)
    check(st in (401, 403), "the image needs a signed-in user or a token", st)
    st, data = raw_get(api, path, token=scanner)
    check(
        st == 200 and hashlib.sha256(data).hexdigest() == r1[0]["app"]["sha256"],
        "the scanner's token fetches the image, intact",
        st,
    )
    st, _ = raw_get(api, f"{P}/firmware/{v(1)}/manifest.json", token=scanner)
    check(st == 404, "only the release's own images are served", st)
    st, _ = raw_get(api, f"{P}/firmware/{v(2)}/{r1[0]['app']['file']}", token=scanner)
    check(st == 404, "each under its own version", st)

    # --- deploying: what is refused
    st, body = deploy(fw_proto["id"])
    check(
        st == 400 and "protocol" in str(body),
        "an incompatible release cannot be deployed",
        (st, body),
    )
    st, body = deploy(fw_old["id"])
    check(
        st == 200 and f"runs {OLD} already" in body["results"][0].get("refused", ""),
        "nor one the scanner runs already",
        body,
    )
    st, body = deploy(fw1["id"], scanners=["nfc-000000000000"])
    st, body = api.call(
        "POST",
        f"{P}/api/fleet/deploy/",
        token=admin,
        body={"firmware": fw1["id"], "scanners": ["nfc-nope"]},
    )
    check(
        st == 400 and "unknown" in str(body),
        "an unknown scanner is refused",
        (st, body),
    )

    # --- the network route: superseding, then a busy scanner, then the update
    st, body = deploy(fw1["id"])
    first = body["results"][0]["deployment"]
    st, body = deploy(fw2["id"])
    second = body["results"][0]["deployment"]
    deps = {d["id"]: d for d in deployments()}
    check(
        deps[first]["state"] == "superseded" and deps[second]["state"] == "pending",
        "a second deployment supersedes one still pending",
        [deps[first]["state"], deps[second]["state"]],
    )

    # A job first: the update waits for it.
    st, job = api.call(
        "POST",
        f"{P}/api/jobs/",
        token=admin,
        body={"location": loc, "scanner": prepare.mid},
    )
    out = sync()
    check(
        not ota_cmds(out) and any(c.get("cmd") == "program" for c in out["cmds"]),
        "while a job is unfinished, the update waits",
        out["cmds"],
    )
    prog = next(c for c in out["cmds"] if c.get("cmd") == "program")
    state["ack"] = prog["seq"]
    say(rsp="program", ok=True, id=job["id"])
    say(evt="failed", id=job["id"], error="cancelled")

    out = sync()
    cmds = ota_cmds(out)
    check(
        len(cmds) == 1
        and cmds[0]["id"] == second
        and cmds[0]["sha256"] == r2[0]["app"]["sha256"]
        and cmds[0]["url"].endswith(f"{P}/firmware/{v(2)}/{r2[0]['app']['file']}"),
        "then the scanner is sent the ota command: the image on this server, and its sha256",
        cmds,
    )
    st, body = api.call(
        "POST",
        f"{P}/api/jobs/",
        token=admin,
        body={"location": loc, "scanner": prepare.mid},
    )
    check(
        st == 400 and "updating" in str(body),
        "no job can be queued while it updates",
        (st, body),
    )

    # Busy: back to pending, tried again later.
    state["ack"] = cmds[0]["seq"]
    say(rsp="ota", ok=False, id=second, error="busy", detail="a job is running")
    d = {x["id"]: x for x in deployments()}[second]
    check(
        d["state"] == "pending" and d["error"] == "busy" and d["attempts"] == 0,
        "a busy scanner puts the update back to pending, without counting an attempt",
        d,
    )
    out = sync()
    check(not ota_cmds(out), "and it is not sent again at once", out["cmds"])

    # Interrupted: the scanner restarts mid-download, still on the old version.
    st, body = deploy(fw1["id"])  # supersedes the busy one; sent now (no retry delay)
    third = body["results"][0]["deployment"]
    out = sync()
    cmds = ota_cmds(out)
    check(
        len(cmds) == 1 and cmds[0]["id"] == third,
        "a new deployment is sent at once",
        cmds,
    )
    state["ack"] = cmds[0]["seq"]
    say(rsp="ota", ok=True, id=third)
    say(evt="ota", state="downloading")
    d = {x["id"]: x for x in deployments()}[third]
    check(
        d["state"] == "downloading"
        and d["via"] == "network"
        and d["from_version"] == OLD,
        "the scanner's progress moves it: downloading",
        d,
    )
    out = restart(OLD)
    d = {x["id"]: x for x in deployments()}[third]
    check(
        d["state"] in ("pending", "sent")
        and d["error"] in ("interrupted", "")
        and d["attempts"] >= 1,
        "a restart mid-download is an interrupted attempt: tried again",
        d,
    )

    # Rollback: it restarts into the new image, which does not hold; the old one comes back.
    out = sync()
    cmds = ota_cmds(out)
    state["ack"] = max([c["seq"] for c in out["cmds"]] + [state["ack"]])
    say(rsp="ota", ok=True, id=third)
    say(evt="ota", state="downloading")
    say(evt="ota", state="restarting")
    restart(OLD)
    d = {x["id"]: x for x in deployments()}[third]
    check(
        d["state"] == "rolled_back" and OLD in d["detail"],
        "coming back on the old version after restarting is a rollback",
        d,
    )

    # Success.
    st, body = deploy(fw2["id"])
    fourth = body["results"][0]["deployment"]
    out = sync()
    cmds = ota_cmds(out)
    state["ack"] = cmds[0]["seq"]
    say(rsp="ota", ok=True, id=fourth)
    say(evt="ota", state="downloading")
    say(evt="ota", state="restarting")
    restart(v(2))
    d = {x["id"]: x for x in deployments()}[fourth]
    check(
        d["state"] == "confirmed" and d["finished_at"],
        "coming back on the new version confirms it",
        d,
    )
    check(
        fleet_scanner()["fw"] == v(2),
        "and the registry shows the new version",
        fleet_scanner(),
    )

    # A failure the scanner reports.
    st, body = deploy(fw1["id"])
    check(
        "older" in body["results"][0].get("refused", ""),
        "a downgrade needs confirming",
        body,
    )
    st, body = deploy(fw1["id"], allow_downgrade=True)
    fifth = body["results"][0]["deployment"]
    out = sync()
    state["ack"] = ota_cmds(out)[0]["seq"]
    say(evt="ota", state="failed", error="bad_image", detail="sha256 mismatch")
    d = {x["id"]: x for x in deployments()}[fifth]
    check(
        d["state"] == "failed" and d["error"] == "bad_image",
        "a confirmed downgrade is sent; a failure it reports ends it",
        d,
    )

    # --- a restart loses the jobs the scanner had taken
    st, job = api.call(
        "POST",
        f"{P}/api/jobs/",
        token=admin,
        body={"location": loc, "scanner": prepare.mid},
    )
    out = sync()
    prog = next(c for c in out["cmds"] if c.get("id") == job["id"])
    state["ack"] = prog["seq"]
    say(rsp="program", ok=True, id=job["id"])
    say(evt="waiting", id=job["id"], timeout_ms=60000)
    restart(v(2))
    st, j = api.call("GET", f"{P}/api/jobs/{job['id']}/", token=admin)
    check(
        j["state"] == "failed" and j["error"] == "scanner_restarted",
        "a job the scanner had taken fails when it restarts",
        j,
    )

    # --- the USB route
    st, fw3 = upload(api, admin, release(v(5)))
    st, body = deploy(fw3["id"])
    usb_dep = body["results"][0]["deployment"]
    st, ci = api.call(
        "POST",
        f"{P}/api/usb/checkin/",
        token=admin,
        body={"reader": READER, "fw": v(2), "proto": 1},
    )
    check(
        st == 200
        and ci["update"]
        and ci["update"]["id"] == usb_dep
        and not ci["update"]["required"]
        and ci["update"]["url"].endswith(
            fw3["version"] + "/" + f"inventree_nfc_scanner-{v(5)}.bin"
        ),
        "a browser checking in is offered the waiting update, deferrable by default",
        ci,
    )
    check(
        fleet_scanner()["last_via"] == "usb",
        "and the registry says it was last seen over USB",
        fleet_scanner(),
    )
    st, body = api.call(
        "POST",
        f"{P}/api/usb/deployments/{usb_dep}/defer/",
        token=admin,
        body={"reader": READER},
    )
    check(st == 200 and body["deferrals"] == 1, "the user may put it off", (st, body))
    st, body = api.call(
        "POST",
        f"{P}/api/usb/deployments/{usb_dep}/defer/",
        token=admin,
        body={"reader": "nfc-000000000001"},
    )
    check(st == 403, "but only for the scanner in hand", st)
    st, body = deploy(fw3["id"], required=True)
    usb_dep = body["results"][0]["deployment"]
    st, ci = api.call(
        "POST",
        f"{P}/api/usb/checkin/",
        token=admin,
        body={"reader": READER, "fw": v(2), "proto": 1},
    )
    check(ci["update"]["required"], "a deployment made required says so", ci["update"])
    st, body = api.call(
        "POST",
        f"{P}/api/usb/deployments/{usb_dep}/defer/",
        token=admin,
        body={"reader": READER},
    )
    check(st == 400, "and cannot be put off", (st, body))
    st, body = api.call(
        "POST",
        f"{P}/api/usb/deployments/{usb_dep}/start/",
        token=admin,
        body={"reader": READER},
    )
    check(
        st == 200 and body["sha256"] == fw3["sha256"],
        "the browser claims it to install it",
        (st, body),
    )
    st, body = api.call(
        "POST",
        f"{P}/api/usb/deployments/{usb_dep}/start/",
        token=admin,
        body={"reader": READER},
    )
    check(st == 400, "only once", st)
    out = sync()
    check(
        not ota_cmds(out),
        "a deployment being installed over USB is not also sent over the network",
        out["cmds"],
    )
    for s in ("downloading", "restarting"):
        st, body = api.call(
            "POST",
            f"{P}/api/usb/deployments/{usb_dep}/report/",
            token=admin,
            body={"reader": READER, "state": s},
        )
    check(body["state"] == "restarting", "the browser reports progress", body)
    st, ci = api.call(
        "POST",
        f"{P}/api/usb/checkin/",
        token=admin,
        body={"reader": READER, "fw": v(5), "proto": 1},
    )
    check(
        ci["update"] is None and ci["last"]["state"] == "confirmed",
        "checking in again on the new version confirms it",
        ci,
    )

    # Interrupted over USB: the browser went away mid-transfer and the scanner reconnects.
    st, fw6 = upload(api, admin, release(v(6)))
    st, body = deploy(fw6["id"])
    dep6 = body["results"][0]["deployment"]
    api.call(
        "POST",
        f"{P}/api/usb/checkin/",
        token=admin,
        body={"reader": READER, "fw": v(5), "proto": 1},
    )
    api.call(
        "POST",
        f"{P}/api/usb/deployments/{dep6}/start/",
        token=admin,
        body={"reader": READER},
    )
    api.call(
        "POST",
        f"{P}/api/usb/deployments/{dep6}/report/",
        token=admin,
        body={"reader": READER, "state": "downloading"},
    )
    st, ci = api.call(
        "POST",
        f"{P}/api/usb/checkin/",
        token=admin,
        body={"reader": READER, "fw": v(5), "proto": 1},
    )
    check(
        ci["update"] and ci["update"]["id"] == dep6,
        "an update cut off over USB is offered again on the next connection",
        ci,
    )
    st, body = api.call(
        "POST", f"{P}/api/fleet/deployments/{dep6}/cancel/", token=admin
    )
    check(
        st == 200 and body["state"] == "cancelled",
        "an admin can withdraw a pending deployment",
        (st, body),
    )

    # --- fetching from GitHub (a stand-in for it)
    fake = FakeGitHub(FAKE_PORT)
    base_url = f"http://host.docker.internal:{FAKE_PORT}"
    saved = {
        k: get_setting(api, admin, k)
        for k in (
            "FIRMWARE_REPO",
            "FIRMWARE_API",
            "FIRMWARE_PRERELEASES",
            "FIRMWARE_KEEP",
            "FIRMWARE_AUTO_DEPLOY",
        )
    }
    try:
        set_setting(api, admin, "FIRMWARE_REPO", "check/firmware")
        set_setting(api, admin, "FIRMWARE_API", base_url)
        set_setting(api, admin, "FIRMWARE_PRERELEASES", False)
        set_setting(api, admin, "FIRMWARE_KEEP", 0)
        g1, g2, g3 = release(v(7)), release(v(8, "-rc.1")), release(v(9))
        fake.publish(g1, base_url=base_url)
        fake.publish(g2, prerelease=True, base_url=base_url)
        fake.publish(g3, bad_digest=True, base_url=base_url)
        st, result = api.call("POST", f"{P}/api/fleet/check/", token=admin)
        check(
            st == 200 and result["added"] == [v(7)],
            "a release on GitHub is fetched; a pre-release is not, by default",
            result,
        )
        check(
            any(v(9) in e and "GitHub" in e for e in result["errors"]),
            "one whose image does not match GitHub's digest is refused",
            result,
        )
        set_setting(api, admin, "FIRMWARE_PRERELEASES", True)
        st, result = api.call("POST", f"{P}/api/fleet/check/", token=admin)
        check(
            result["added"] == [v(8, "-rc.1")],
            "with pre-releases on, it is fetched too",
            result,
        )
        st, overview = api.call("GET", f"{P}/api/fleet/", token=admin)
        check(
            overview["last_check"] and overview["last_check"]["at"],
            "the last check is shown",
            overview["last_check"],
        )
        fetched = next(f for f in overview["firmware"] if f["version"] == v(7))
        check(
            fetched["source"] == "github"
            and fetched["release_url"].endswith(f"v{v(7)}"),
            "with where it came from",
            fetched,
        )

        # Automatic deployment: the newest release this plugin can drive goes out; a newer one
        # it cannot (it needs a newer plugin) is held, shown, and not deployed; and nothing
        # about it breaks the check. It goes to every scanner running something older, real
        # ones included, so it runs only when asked (--auto-deploy) and with them off the
        # network; what it gave them is withdrawn at once.
        if not run.auto_deploy:
            print(
                "skip automatic deployment (it would reach every scanner here; see --auto-deploy)"
            )
        else:
            set_setting(api, admin, "FIRMWARE_AUTO_DEPLOY", True)
            fake.publish(release(v(10)), base_url=base_url)
            fake.publish(release(v(11), min_plugin="99.0.0"), base_url=base_url)
            st, result = api.call("POST", f"{P}/api/fleet/check/", token=admin)
            check(
                st == 200 and sorted(result["added"]) == sorted([v(10), v(11)]),
                "with automatic deployment on, a check that fetches a release the plugin cannot drive still succeeds",
                (st, result),
            )
            check(
                (result.get("auto_deploy") or {}).get("version") == v(10),
                "and deploys the newest one it can drive instead",
                result.get("auto_deploy"),
            )
            d = deployments()[0]
            check(
                d["version"] == v(10)
                and d["state"] == "pending"
                and d["requested_by"] is None,
                "to the scanner running something older, as an automatic deployment",
                d,
            )
            st, overview = api.call("GET", f"{P}/api/fleet/", token=admin)
            check(
                overview["newest"] == v(10) and overview["last_check"]["added"],
                "the fleet page offers the newest it can drive, and the check is recorded",
                (overview["newest"], overview["last_check"]),
            )
            incompatible = next(
                f for f in overview["firmware"] if f["version"] == v(11)
            )
            check(
                incompatible["incompatible"]
                and "99.0.0" in incompatible["incompatible"],
                "the newer one is listed, marked incompatible",
                incompatible,
            )

            fake.publish(release(v(12), proto=2), base_url=base_url)
            st, result = api.call("POST", f"{P}/api/fleet/check/", token=admin)
            check(
                st == 200
                and result["added"] == [v(12)]
                and "auto_deploy" not in result,
                "a check that fetches only a release it cannot drive deploys nothing",
                (st, result),
            )
            api.call(
                "POST", f"{P}/api/fleet/deployments/{d['id']}/cancel/", token=admin
            )
            st, everyone = api.call("GET", f"{P}/api/fleet/deployments/", token=admin)
            stray = [
                x for x in everyone if x["reader"] != READER and x["version"] == v(10)
            ]
            for x in stray:
                api.call(
                    "POST", f"{P}/api/fleet/deployments/{x['id']}/cancel/", token=admin
                )
            check(
                all(x["state"] == "pending" for x in stray),
                f"the automatic deployment it made to {len(stray)} other scanner(s) never got further than pending, and is withdrawn",
                stray,
            )
    finally:
        for k, val in saved.items():
            if val is not None:
                set_setting(api, admin, k, val)
        fake.server.shutdown()

    # --- clean up
    st, _ = api.call("DELETE", f"{P}/api/fleet/scanners/{READER}/", token=admin)
    check(st == 204, "an admin can forget a scanner", st)
    st, overview = api.call("GET", f"{P}/api/fleet/", token=admin)
    for f in overview["firmware"]:
        if f["version"].startswith((f"90.{stamp}.", f"89.{stamp}.")):
            api.call("DELETE", f"{P}/api/fleet/firmware/{f['id']}/", token=admin)
    st, overview = api.call("GET", f"{P}/api/fleet/", token=admin)
    # A release some other scanner's history refers to (an automatic deployment, withdrawn)
    # keeps its record; its images go all the same.
    left = [
        f
        for f in overview["firmware"]
        if f["version"].startswith((f"90.{stamp}.", f"89.{stamp}.")) and f["available"]
    ]
    check(not left, "and remove releases", [f["version"] for f in left])

    # Leave nothing on show: its machine off the dashboard, and a release check of the real
    # repository recorded in place of the stand-in's.
    st, _ = api.call(
        "PATCH", f"/api/machine/{prepare.mid}/", token=admin, body={"active": False}
    )
    check(
        st == 200,
        "its machine is deactivated, so it leaves no offline scanner behind",
        st,
    )
    st, result = api.call("POST", f"{P}/api/fleet/check/", token=admin)
    check(
        st == 200
        and not any("check/firmware" in e or "90." in e for e in result["errors"]),
        "and a check of the real repository replaces the stand-in's on the fleet page",
        (st, result),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=f"http://127.0.0.1:{HTTP_PORT}")
    ap.add_argument("--host", default=f"inventree.localhost:{HTTP_PORT}")
    ap.add_argument(
        "--auto-deploy",
        action="store_true",
        help="also check automatic deployment, which reaches every scanner on this server: take "
        "real ones off the network first (nfcprog.py net disable)",
    )
    args = ap.parse_args()
    run.auto_deploy = args.auto_deploy
    with open(os.path.join(HERE, "admin.token")) as f:
        admin = f.read().strip()
    api = Api(args.base, args.host)
    scanner, mid, loc = prepare(api, admin)
    prepare.mid = mid
    run(api, admin, scanner, loc)
    print()
    print(
        "all fleet checks passed"
        if base.fails == 0
        else f"{base.fails} fleet checks FAILED"
    )
    sys.exit(1 if base.fails else 0)


if __name__ == "__main__":
    main()
