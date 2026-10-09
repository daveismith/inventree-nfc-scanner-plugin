"""Made-up firmware releases, shaped as the firmware's tools/make_release.py makes them.

Shared by the server and browser layers; no Django here.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass


@dataclass
class Release:
    manifest: dict
    app: bytes
    merged: bytes

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def app_file(self) -> str:
        return self.manifest["app"]["file"]

    @property
    def sha256(self) -> str:
        return self.manifest["app"]["sha256"]


def release(
    version,
    size=4096,
    proto=1,
    settings_version=1,
    min_plugin="0.1.0",
    target="esp32s3",
) -> Release:
    app = secrets.token_bytes(size)
    merged = b"\xff" * 64 + app
    name = f"inventree_nfc_scanner-{version}"
    manifest = {
        "name": "inventree_nfc_scanner",
        "version": version,
        "target": target,
        "idf": "v6.1",
        "proto": proto,
        "settings_version": settings_version,
        "min_plugin": min_plugin,
        "git_sha": "0" * 40,
        "dev": False,
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
    return Release(manifest, app, merged)
