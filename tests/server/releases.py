"""Made-up firmware releases, shaped as the firmware's tools/make_release.py makes them."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass

from django.core.files.uploadedfile import SimpleUploadedFile

from .scanner import P


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


def upload(
    client,
    rel: Release,
    *,
    app: bytes | None = None,
    manifest: dict | None = None,
    files=None,
):
    """POST the release to the fleet page's upload, as an admin's browser does."""
    if files is None:
        manifest = manifest or rel.manifest
        files = {
            "manifest": SimpleUploadedFile(
                "manifest.json", json.dumps(manifest).encode()
            ),
            "app": SimpleUploadedFile(rel.app_file, rel.app if app is None else app),
            "merged": SimpleUploadedFile(manifest["merged"]["file"], rel.merged),
        }
    return client.post(f"{P}/api/fleet/upload/", files, format="multipart")


class FakeGitHub:
    """GitHub's releases API, as `responses` mocks of the requests the plugin makes."""

    API = "https://github.test"
    REPO = "check/firmware"

    def __init__(self, mock):
        self.mock = mock
        self.releases: list[dict] = []
        mock.add_callback(
            "GET",
            f"{self.API}/repos/{self.REPO}/releases",
            callback=lambda req: (
                200,
                {"Content-Type": "application/json"},
                json.dumps(self.releases),
            ),
        )

    def publish(
        self,
        rel: Release,
        *,
        prerelease=False,
        bad_digest=False,
        draft=False,
        tag=None,
        omit=(),
    ):
        assets = []
        for name, data in (
            ("manifest.json", json.dumps(rel.manifest).encode()),
            (rel.app_file, rel.app),
            (rel.manifest["merged"]["file"], rel.merged),
        ):
            if name in omit:
                continue
            url = f"{self.API}/assets/{rel.version}/{name}"
            self.mock.add(
                "GET", url, body=data, content_type="application/octet-stream"
            )
            digest = (
                "0" * 64
                if bad_digest and name == rel.app_file
                else hashlib.sha256(data).hexdigest()
            )
            assets.append({
                "name": name,
                "size": len(data),
                "url": url,
                "digest": f"sha256:{digest}",
            })
        self.releases.insert(
            0,
            {
                "tag_name": tag or f"v{rel.version}",
                "draft": draft,
                "prerelease": prerelease,
                "html_url": f"https://github.com/{self.REPO}/releases/tag/v{rel.version}",
                "published_at": "2026-10-08T00:00:00Z",
                "assets": assets,
            },
        )
