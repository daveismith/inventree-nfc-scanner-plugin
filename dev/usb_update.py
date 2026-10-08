#!/usr/bin/env python3
"""Do what the browser does with a USB scanner that has an update waiting, from the command
line, against the local InvenTree (dev/README.md) and a scanner on this computer's USB.

    python3 usb_update.py --port /dev/cu.usbmodem…  [--defer] [--token-file admin.token]

Reads the scanner's `info`, checks it in (api/usb/checkin/), and if an update is offered,
claims it, fetches the image from the server, checks its size and sha256, streams it over the
serial link (ota_begin, ota_data, ota_end), reports progress, waits for the scanner to come
back, checks in again and prints the verdict. With --defer it chooses "Later" instead.
The same sequence as frontend/src/scanner.ts.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.request

import serial

HERE = os.path.dirname(os.path.abspath(__file__))
P = "/plugin/nfcscanner"
CHUNK = 768


class Link:
    def __init__(self, port):
        self.port = serial.Serial(port, 115200, timeout=0.1)
        self.port.dtr = True
        self.buf = b""

    def request(self, obj, seconds=10.0):
        self.port.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.buf += self.port.read(max(1, self.port.in_waiting))
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("rsp") == obj["cmd"]:
                    return msg
        raise TimeoutError(f"no answer to {obj['cmd']}")

    def close(self):
        try:
            self.port.close()
        except OSError:
            pass


def call(base, host, token, method, path, body=None, raw=False):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Host": host,
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        return data if raw else json.loads(data or b"null")


def wait_for_port(path, seconds=40):
    deadline = time.monotonic() + seconds
    time.sleep(2)  # it goes away first
    while time.monotonic() < deadline:
        if os.path.exists(path):
            try:
                return Link(path)
            except (OSError, serial.SerialException):
                pass
        time.sleep(0.5)
    sys.exit("the scanner did not come back")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--host", default="inventree.localhost:8080")
    ap.add_argument("--token-file", default=os.path.join(HERE, "admin.token"))
    ap.add_argument("--defer", action="store_true")
    args = ap.parse_args()
    with open(args.token_file) as f:
        token = f.read().strip()
    api = lambda *a, **k: call(args.base, args.host, token, *a, **k)  # noqa: E731

    link = Link(args.port)
    time.sleep(0.6)  # the scanner says hello a moment after the port opens
    info = link.request({"cmd": "info"})
    reader = info.get("reader")
    print(f"scanner {reader} runs {info.get('fw')}")
    ci = api(
        "POST",
        f"{P}/api/usb/checkin/",
        {"reader": reader, "fw": info["fw"], "proto": info["proto"]},
    )
    offer = ci["update"]
    if not offer:
        print("no update waiting; last:", ci["last"])
        return 0
    print(f"offered {offer['version']} (required: {offer['required']})")
    if args.defer:
        print(
            "deferred:",
            api(
                "POST",
                f"{P}/api/usb/deployments/{offer['id']}/defer/",
                {"reader": reader},
            ),
        )
        return 0

    go = api(
        "POST", f"{P}/api/usb/deployments/{offer['id']}/start/", {"reader": reader}
    )
    api(
        "POST",
        f"{P}/api/usb/deployments/{go['id']}/report/",
        {"reader": reader, "state": "downloading"},
    )
    image = api("GET", go["url"], raw=True)
    assert (
        len(image) == go["size"] and hashlib.sha256(image).hexdigest() == go["sha256"]
    ), "image mismatch"
    started = time.monotonic()
    rsp = link.request({
        "cmd": "ota_begin",
        "id": go["id"],
        "size": len(image),
        "sha256": go["sha256"],
    })
    assert rsp.get("ok"), rsp
    for at in range(0, len(image), CHUNK):
        rsp = link.request({
            "cmd": "ota_data",
            "id": go["id"],
            "at": at,
            "data": base64.b64encode(image[at : at + CHUNK]).decode(),
        })
        if not rsp.get("ok"):
            api(
                "POST",
                f"{P}/api/usb/deployments/{go['id']}/report/",
                {
                    "reader": reader,
                    "state": "failed",
                    "error": rsp.get("error", "failed"),
                    "detail": rsp.get("detail", ""),
                },
            )
            sys.exit(f"refused: {rsp}")
    rsp = link.request({"cmd": "ota_end", "id": go["id"]}, seconds=30)
    assert rsp.get("ok"), rsp
    print(f"sent {len(image)} bytes in {time.monotonic() - started:.1f}s")
    api(
        "POST",
        f"{P}/api/usb/deployments/{go['id']}/report/",
        {"reader": reader, "state": "restarting"},
    )
    link.close()

    link = wait_for_port(args.port)
    time.sleep(0.6)
    info = link.request({"cmd": "info"})
    print(f"back, running {info.get('fw')}")
    ci = api(
        "POST",
        f"{P}/api/usb/checkin/",
        {"reader": reader, "fw": info["fw"], "proto": info["proto"]},
    )
    print("verdict:", ci["last"])
    link.close()
    return 0 if ci["last"] and ci["last"]["state"] == "confirmed" else 1


if __name__ == "__main__":
    sys.exit(main())
