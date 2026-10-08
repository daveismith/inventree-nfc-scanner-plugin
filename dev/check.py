#!/usr/bin/env python3
"""Checks of the plugin's API against the local InvenTree (dev/README.md).

Needs `admin.token` from setup.sh. Makes, or reuses, a user and token for a scanner, an
"NFC Scanner" machine with the network driver, and a stock location; then plays a scanner
through /sync for a whole job and checks what the browser-side API reports at each step.
Leaves `scanner.token` and `machine.id` behind for tools/sync_bridge.py.

    python3 check.py [--base http://127.0.0.1:8080] [--host inventree.localhost:8080]
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
READER = "nfc-34b7da52a084"


def env_value(name, default=""):
    """A value from dev/.env, the compose environment."""
    try:
        with open(os.path.join(HERE, ".env")) as f:
            for line in f:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except FileNotFoundError:
        pass
    return default


ADMIN_USER = env_value("INVENTREE_ADMIN_USER", "admin")
ADMIN_PASSWORD = env_value("INVENTREE_ADMIN_PASSWORD", "admin-nfc-dev")
SITE_URL = env_value("INVENTREE_SITE_URL", "http://inventree.localhost:8080")
HTTP_PORT = env_value("INVENTREE_HTTP_PORT", "8080")
SCANNER_USER, SCANNER_PASSWORD = "scanner-desk", "scanner-nfc-dev"

fails = 0


def check(cond, what, got=None):
    global fails
    print(("ok   " if cond else "FAIL ") + what + ("" if cond else f"   got: {got}"))
    fails += not cond


class Api:
    def __init__(self, base, host):
        self.base, self.host = base, host

    def call(self, method, path, body=None, token=None, basic=None, timeout=60):
        headers = {
            "Host": self.host,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Token {token}"
        if basic:
            import base64

            headers["Authorization"] = (
                "Basic " + base64.b64encode(basic.encode()).decode()
            )
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if "json" not in (r.headers.get("Content-Type") or ""):
                    return r.status, raw[:200]  # a static file, say
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw[:300]


def prepare(api, admin):
    """A scanner user with a token, a machine for it, and a location. Idempotent."""
    st, users = api.call("GET", f"/api/user/?search={SCANNER_USER}", token=admin)
    user = next((u for u in users if u["username"] == SCANNER_USER), None)
    if user is None:
        st, user = api.call(
            "POST",
            "/api/user/",
            token=admin,
            body={
                "username": SCANNER_USER,
                "first_name": "Desk",
                "last_name": "Scanner",
                "email": "scanner@example.com",
                "password": SCANNER_PASSWORD,
            },
        )
        assert st == 201, user
    api.call(
        "PUT",
        f"/api/user/{user['pk']}/set-password/",
        token=admin,
        body={"password": SCANNER_PASSWORD, "override_warning": True},
    )
    st, tok = api.call(
        "GET",
        "/api/user/token/?name=nfc-dev",
        basic=f"{SCANNER_USER}:{SCANNER_PASSWORD}",
    )
    assert st == 200 and "token" in tok, tok
    scanner_token = tok["token"]

    st, machines = api.call("GET", "/api/machine/", token=admin)
    machine = next((m for m in machines if m["name"] == "Desk scanner"), None)
    if machine is None:
        st, machine = api.call(
            "POST",
            "/api/machine/",
            token=admin,
            body={
                "name": "Desk scanner",
                "machine_type": "nfc-scanner",
                "driver": "nfc-network",
                "active": True,
            },
        )
        assert st == 201, machine
    mid = machine["pk"]
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
    loc = next((x for x in locations if x["name"] == "Bin A1"), None)
    if loc is None:
        st, loc = api.call(
            "POST",
            "/api/stock/location/",
            token=admin,
            body={"name": "Bin A1", "description": "test bin"},
        )
        assert st == 201, loc
    # A fresh location has no barcode; the job test links one. Unlink so the test repeats.
    api.call(
        "POST", "/api/barcode/unlink/", token=admin, body={"stocklocation": loc["pk"]}
    )

    with open(os.path.join(HERE, "scanner.token"), "w") as f:
        f.write(scanner_token + "\n")
    with open(os.path.join(HERE, "machine.id"), "w") as f:
        f.write(str(mid) + "\n")
    return scanner_token, str(mid), loc["pk"]


def run(api, admin, scanner, machine, loc):
    P = "/plugin/nfcscanner"
    boot = int(time.time()) % 100000

    def sync(body, token=scanner):
        return api.call(
            "POST",
            f"{P}/sync/",
            token=token,
            body={
                "reader": READER,
                "boot": boot,
                "proto": 1,
                "ack": 0,
                "wait_s": 0,
                "msgs": [],
                **body,
            },
        )

    # --- access
    st, body = sync({}, token="bad-token")
    check(st == 401, "wrong token: 401", (st, body))
    st, body = sync({}, token=admin)
    check(st == 403, "a token that is not the machine's user: 403", (st, body))
    st, body = api.call(
        "POST",
        f"{P}/sync/",
        token=scanner,
        body={"reader": "nfc-000000000000", "proto": 1},
    )
    check(
        st == 403,
        "unknown reader id: 403, the same as a reader that is not this token's",
        (st, body),
    )
    st, body = sync({"boot": "x"})
    check(st == 400, "a malformed sync body: 400", (st, body))
    st, body = sync({"msgs": "nope"})
    check(st == 400, "msgs that is not a list: 400", (st, body))
    st, body = api.call(
        "GET", f"{P}/api/location/{loc}/tag/", basic=f"{ADMIN_USER}:{ADMIN_PASSWORD}"
    )
    check(
        st == 401,
        "basic auth is not accepted on plugin URLs (session or token only)",
        st,
    )
    st, body = api.call("GET", f"{P}/api/location/{loc}/tag/", token=scanner)
    check(
        st == 403,
        "the scanner user, with no stock permission, may not read the tag data (nor the password)",
        (st, body),
    )
    st, body = api.call("GET", f"{P}/api/scanners/", token=scanner)
    check(st == 403, "nor list the scanners", st)

    # --- the tag's contents
    st, tag = api.call("GET", f"{P}/api/location/{loc}/tag/", token=admin)
    check(
        st == 200
        and tag["text"] == f"INV-SL{loc}"
        and tag["uri"].endswith(f"/web/stock/location/{loc}")
        and tag["ndef"].startswith("9101"),
        "tag data: text, URI and NDEF for the location",
        tag,
    )

    # --- idle sync; drain whatever an earlier run left
    st, body = sync({"ack": 10**6})
    check(st == 200 and body["cmds"] == [], "idle sync: nothing to do", (st, body))
    st, scanners = api.call("GET", f"{P}/api/scanners/", token=admin)
    sc = next(s for s in scanners if s["id"] == machine)
    check(
        sc["online"] and sc["status"] == "online",
        "scanner shows online after a sync",
        sc,
    )

    # --- a job, start to finish
    st, job = api.call(
        "POST",
        f"{P}/api/jobs/",
        token=admin,
        body={"location": loc, "scanner": machine},
    )
    check(st == 201 and job["state"] == "queued", "job created, queued", (st, job))
    jid = job["id"]
    st, body = sync({"ack": 0})
    cmds = body["cmds"]
    check(
        len(cmds) == 1
        and cmds[0]["cmd"] == "program"
        and cmds[0]["id"] == jid
        and "ndef" in cmds[0]
        and cmds[0]["timeout_ms"] > 0,
        "sync hands out the program command",
        body,
    )
    seq = cmds[0]["seq"]
    st, j = api.call("GET", f"{P}/api/jobs/{jid}/", token=admin)
    check(j["state"] == "sent", "job: sent", j["state"])
    st, body = sync({"ack": 0})
    check(
        len(body["cmds"]) == 1 and body["cmds"][0]["seq"] == seq,
        "an unacknowledged command is repeated",
        body,
    )

    st, body = sync({
        "ack": seq,
        "msgs": [
            {"seq": 1, "rsp": "program", "ok": True, "id": jid},
            {"seq": 2, "evt": "waiting", "id": jid, "timeout_ms": 60000},
        ],
    })
    check(
        body == {"ack": 2, "cmds": []},
        "acknowledged; messages applied; queue empty",
        body,
    )
    st, j = api.call("GET", f"{P}/api/jobs/{jid}/", token=admin)
    check(j["state"] == "waiting", "job: waiting", j["state"])
    st, scanners = api.call("GET", f"{P}/api/scanners/", token=admin)
    check(
        next(s for s in scanners if s["id"] == machine)["status"] == "busy",
        "scanner shows busy while a job waits",
    )

    sync({
        "ack": seq,
        "msgs": [{"seq": 3, "evt": "writing", "id": jid, "uid": "04A1B2C3D4E5F6"}],
    })
    st, j = api.call("GET", f"{P}/api/jobs/{jid}/", token=admin)
    check(
        j["state"] == "writing" and j["uid"] == "04A1B2C3D4E5F6",
        "job: writing, with the UID",
        j,
    )

    done = {
        "seq": 4,
        "evt": "done",
        "id": jid,
        "uid": "04A1B2C3D4E5F6",
        "type": "ntag215",
        "protected": False,
    }
    st, body = sync({"ack": seq, "msgs": [done]})
    st, j = api.call("GET", f"{P}/api/jobs/{jid}/", token=admin)
    check(
        j["state"] == "done" and j["tag_type"] == "ntag215" and j["finished_at"],
        "job: done",
        j,
    )
    st, location = api.call("GET", f"/api/stock/location/{loc}/", token=admin)
    check(
        bool(location.get("barcode_hash")),
        "the UID is linked to the location as its barcode",
        location.get("barcode_hash"),
    )
    st, body = sync({"ack": seq, "msgs": [done]})
    st, j2 = api.call("GET", f"{P}/api/jobs/{jid}/", token=admin)
    check(
        body["ack"] == 4 and j2["finished_at"] == j["finished_at"],
        "a repeated message is acknowledged and not applied twice",
    )

    # --- a tap outside a job
    sync({
        "ack": seq,
        "msgs": [
            {
                "seq": 5,
                "evt": "tag",
                "uid": "04AABBCCDDEEFF",
                "type": "ntag215",
                "text": f"INV-SL{loc}",
                "protected": True,
            }
        ],
    })
    st, scanners = api.call("GET", f"{P}/api/scanners/", token=admin)
    sc = next(s for s in scanners if s["id"] == machine)
    check(
        sc["last_tag"] and sc["last_tag"]["text"] == f"INV-SL{loc}",
        "a tap is kept as the last tag",
        sc["last_tag"],
    )

    # --- refused: not_blank
    st, job = api.call(
        "POST",
        f"{P}/api/jobs/",
        token=admin,
        body={"location": loc, "scanner": machine},
    )
    jid2 = job["id"]
    st, body = sync({"ack": seq})
    seq2 = body["cmds"][0]["seq"]
    sync({
        "ack": seq2,
        "msgs": [
            {"seq": 6, "rsp": "program", "ok": True, "id": jid2},
            {
                "seq": 7,
                "evt": "failed",
                "id": jid2,
                "error": "not_blank",
                "uid": "04A1B2C3D4E5F6",
                "text": "INV-SL9",
                "uri": "http://x/web/stock/location/9",
            },
        ],
    })
    st, j = api.call("GET", f"{P}/api/jobs/{jid2}/", token=admin)
    check(
        j["state"] == "failed"
        and j["error"] == "not_blank"
        and j["existing_text"] == "INV-SL9",
        "refused job: failed, not_blank, with what the tag holds",
        j,
    )

    # --- cancel, before and after collection
    st, job = api.call(
        "POST",
        f"{P}/api/jobs/",
        token=admin,
        body={"location": loc, "scanner": machine, "overwrite": True},
    )
    st, j = api.call("POST", f"{P}/api/jobs/{job['id']}/cancel/", token=admin)
    check(
        j["state"] == "cancelled",
        "a queued job is cancelled before the scanner sees it",
        j,
    )
    st, body = sync({"ack": seq2})
    check(body["cmds"] == [], "and nothing is handed out for it", body)
    st, job = api.call(
        "POST",
        f"{P}/api/jobs/",
        token=admin,
        body={"location": loc, "scanner": machine, "overwrite": True},
    )
    st, body = sync({"ack": seq2})
    check(
        body["cmds"][0]["id"] == job["id"] and body["cmds"][0].get("overwrite") is True,
        "the overwrite flag is carried",
        body,
    )
    seq3 = body["cmds"][0]["seq"]
    api.call("POST", f"{P}/api/jobs/{job['id']}/cancel/", token=admin)
    st, body = sync({"ack": seq3})
    check(
        len(body["cmds"]) == 1
        and body["cmds"][0]["cmd"] == "cancel"
        and body["cmds"][0]["id"] == job["id"],
        "a collected job is cancelled by a cancel command",
        body,
    )
    sync({
        "ack": body["cmds"][0]["seq"],
        "msgs": [{"seq": 8, "evt": "failed", "id": job["id"], "error": "cancelled"}],
    })
    st, j = api.call("GET", f"{P}/api/jobs/{job['id']}/", token=admin)
    check(
        j["state"] == "cancelled",
        "and ends cancelled when the scanner confirms",
        j["state"],
    )

    # --- a scanner that restarts (ack back at 0) is not handed a finished job's command again
    st, job = api.call(
        "POST",
        f"{P}/api/jobs/",
        token=admin,
        body={"location": loc, "scanner": machine},
    )
    st, reply = sync({"boot": boot + 1})
    cmd = next(c for c in reply["cmds"] if c.get("id") == job["id"])
    sync({
        "boot": boot + 1,
        "ack": cmd["seq"],
        "msgs": [
            {"seq": 1, "rsp": "program", "ok": True, "id": job["id"]},
            {
                "seq": 2,
                "evt": "done",
                "id": job["id"],
                "uid": "04A1B2C3D4E5F6",
                "type": "ntag215",
                "protected": False,
            },
        ],
    })
    st, reply = sync({"boot": boot + 2})
    check(
        not any(c.get("id") == job["id"] for c in reply["cmds"]),
        "a restarted scanner is not sent a finished job again",
        reply,
    )

    # --- a tag re-assigned to another bin takes its barcode with it
    st, loc2 = api.call(
        "POST",
        "/api/stock/location/",
        token=admin,
        body={"name": "Bin A2 (check)", "description": "test bin"},
    )
    if st != 201:
        st, found = api.call("GET", "/api/stock/location/?search=Bin%20A2", token=admin)
        loc2 = next(x for x in found if x["name"] == "Bin A2 (check)")
    st, body = api.call(
        "POST",
        f"{P}/api/location/{loc2['pk']}/link/",
        token=admin,
        body={"uid": "04A1B2C3D4E5F6"},
    )
    check(
        st == 200 and body["outcome"].startswith("moved from"),
        "the same UID linked to another bin is moved there",
        (st, body),
    )
    st, body = api.call(
        "POST",
        f"{P}/api/location/{loc2['pk']}/link/",
        token=admin,
        body={"uid": "04A1B2C3D4E5F6"},
    )
    check(
        st == 200 and body["outcome"] == "already linked",
        "linking it again is a no-op",
        (st, body),
    )
    st, body = api.call(
        "POST",
        f"{P}/api/location/{loc2['pk']}/link/",
        token=admin,
        body={"uid": "not hex"},
    )
    check(st == 400, "a UID that is not hex is refused", (st, body))
    st, location = api.call("GET", f"/api/stock/location/{loc}/", token=admin)
    check(
        not location.get("barcode_hash"),
        "and the first bin no longer carries it",
        location.get("barcode_hash"),
    )
    st, body = api.call(
        "POST",
        f"{P}/api/location/{loc}/link/",
        token=admin,
        body={"uid": "04A1B2C3D4E5F6"},
    )
    check(
        st == 200 and body["outcome"].startswith("moved from"),
        "and back again",
        (st, body),
    )

    # --- the USB route's record
    st, j = api.call(
        "POST",
        f"{P}/api/location/{loc}/jobs/usb/",
        token=admin,
        body={
            "state": "done",
            "uid": "04A1B2C3D4E5F6",
            "tag_type": "ntag215",
            "protected": True,
        },
    )
    check(
        st == 201 and j["scanner"] is None and j["state"] == "done",
        "a USB job is recorded with no scanner",
        (st, j),
    )

    # --- the panel is offered on a stock location page, with its bundle served
    st, feats = api.call(
        "GET",
        f"/api/plugins/ui/features/panel/?target_model=stocklocation&target_id={loc}",
        token=admin,
    )
    panel = next((f for f in feats if f["plugin_name"] == "nfcscanner"), None)
    check(
        panel
        and panel["source"].endswith(":RenderNfcPanel")
        and str(panel["context"]["location"]) == str(loc),
        "the NFC tag panel is offered for the location",
        feats,
    )
    if panel:
        st, _ = api.call("GET", panel["source"].split(":")[0], token=admin)
        check(st == 200, "the panel's JavaScript is served", st)

    # --- long polling: held until a job is queued; an empty hold ends at wait_s
    api.call(
        "PATCH",
        "/api/plugins/nfcscanner/settings/LONG_POLL/",
        token=admin,
        body={"value": "True"},
    )
    try:
        result = {}

        def held():
            t0 = time.monotonic()
            result["body"] = sync({"ack": 10**6, "wait_s": 20})[1]
            result["took"] = time.monotonic() - t0

        t = threading.Thread(target=held)
        t.start()
        time.sleep(2.0)
        st, job = api.call(
            "POST",
            f"{P}/api/jobs/",
            token=admin,
            body={"location": loc, "scanner": machine, "overwrite": True},
        )
        t.join()
        cmds = result["body"]["cmds"]
        check(
            len(cmds) == 1
            and cmds[0]["id"] == job["id"]
            and 2.0 <= result["took"] < 5.0,
            f"a held request returns the job as it is queued ({result['took']:.1f}s in, asked to wait 20)",
            result,
        )
        api.call("POST", f"{P}/api/jobs/{job['id']}/cancel/", token=admin)
        sync({
            "ack": 10**6,
            "msgs": [
                {"seq": 9, "evt": "failed", "id": job["id"], "error": "cancelled"}
            ],
        })
        t0 = time.monotonic()
        st, body = sync({"ack": 10**6, "wait_s": 3})
        took = time.monotonic() - t0
        check(
            2.8 <= took < 5.0 and body["cmds"] == [],
            f"an empty hold ends after wait_s ({took:.1f}s for 3)",
            (took, body),
        )
    finally:
        api.call(
            "PATCH",
            "/api/plugins/nfcscanner/settings/LONG_POLL/",
            token=admin,
            body={"value": "False"},
        )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base", default=f"http://127.0.0.1:{HTTP_PORT}")
    ap.add_argument("--host", default=SITE_URL.split("://", 1)[-1])
    args = ap.parse_args()
    try:
        with open(os.path.join(HERE, "admin.token")) as f:
            admin = f.read().strip()
    except FileNotFoundError:
        sys.exit("run setup.sh first (it writes admin.token)")
    api = Api(args.base, args.host)
    scanner, machine, loc = prepare(api, admin)
    run(api, admin, scanner, machine, loc)
    print(f"\n{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
