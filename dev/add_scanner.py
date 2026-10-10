#!/usr/bin/env python3
"""Set a scanner up as a network scanner on the local InvenTree (dev/README.md).

Makes, or reuses, a user for the scanner with an API token, and an "NFC Scanner" machine with
the network driver, configured with the scanner's reader id and that user. Leaves the token in
`scanner.token` and the machine's id in `machine.id`, for the firmware's tools/sync_bridge.py,
or to give the scanner over USB (`nfcprog.py net server URL`, which asks for the token).

    python3 add_scanner.py [--reader nfc-34b7da52a084] [--name "Desk scanner"]

Needs `admin.token` from setup.sh. The reader id is the scanner's: `nfcprog.py info` shows it.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


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


SITE_URL = env_value("INVENTREE_SITE_URL", "http://inventree.localhost:8080")
HTTP_PORT = env_value("INVENTREE_HTTP_PORT", "8080")
SCANNER_USER, SCANNER_PASSWORD = "scanner-desk", "scanner-nfc-dev"


class Api:
    def __init__(self, base, host):
        self.base, self.host = base, host

    def call(self, method, path, body=None, token=None, basic=None):
        headers = {
            "Host": self.host,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Token {token}"
        if basic:
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
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw[:300]


def add_scanner(api, admin, reader, name):
    """The user, its token and the machine. Idempotent: run it again to change the reader id."""
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

    st, machines = api.call("GET", "/api/machine/", token=admin)
    machine = next((m for m in machines if m["name"] == name), None)
    if machine is None:
        st, machine = api.call(
            "POST",
            "/api/machine/",
            token=admin,
            body={
                "name": name,
                "machine_type": "nfc-scanner",
                "driver": "nfc-network",
                "active": True,
            },
        )
        assert st == 201, machine
    mid = machine["pk"]
    api.call("PATCH", f"/api/machine/{mid}/", token=admin, body={"active": True})
    for key, value in (("READER_ID", reader), ("USER", str(user["pk"]))):
        api.call(
            "PUT",
            f"/api/machine/{mid}/settings/D/{key}/",
            token=admin,
            body={"value": value},
        )
    api.call("POST", f"/api/machine/{mid}/restart/", token=admin)
    time.sleep(2)

    with open(os.path.join(HERE, "scanner.token"), "w") as f:
        f.write(tok["token"] + "\n")
    with open(os.path.join(HERE, "machine.id"), "w") as f:
        f.write(str(mid) + "\n")
    return mid


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--reader",
        default="nfc-34b7da52a084",
        help="the scanner's reader id (nfcprog.py info)",
    )
    ap.add_argument(
        "--name", default="Desk scanner", help="the machine's name in InvenTree"
    )
    ap.add_argument("--base", default=f"http://127.0.0.1:{HTTP_PORT}")
    ap.add_argument("--host", default=SITE_URL.split("://", 1)[-1])
    args = ap.parse_args()
    try:
        with open(os.path.join(HERE, "admin.token")) as f:
            admin = f.read().strip()
    except FileNotFoundError:
        sys.exit("run setup.sh first (it writes admin.token)")
    mid = add_scanner(Api(args.base, args.host), admin, args.reader, args.name)
    print(
        f'"{args.name}" (machine {mid}) is reader {args.reader}, user {SCANNER_USER}; token in scanner.token'
    )


if __name__ == "__main__":
    main()
