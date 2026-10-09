"""Uploading made-up releases (tests/firmware_release.py), and a stand-in for GitHub's API."""

from __future__ import annotations

import hashlib
import json

from django.core.files.uploadedfile import SimpleUploadedFile

from ..firmware_release import Release, release  # noqa: F401 (re-exported for the tests)
from .scanner import P


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
