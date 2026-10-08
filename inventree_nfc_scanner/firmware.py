"""Scanner firmware: the releases the server holds, where they come from, how they are checked.

A release is three files published by the firmware repository's tag workflow
(tools/make_release.py there): the app image an update installs, a merged image for flashing
a blank board, and `manifest.json`, which describes both. The server fetches them from the
repository's GitHub releases (on a schedule, and on demand), or an admin uploads them.

Integrity is one sha256, carried end to end: GitHub's own digest of each asset, the
manifest's, the bytes downloaded, the command sent to the scanner, and the scanner's check of
what it wrote before it restarts into it. There is no signing.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from . import PLUGIN_VERSION
from .models import Deployment, Firmware

logger = logging.getLogger("inventree")

NAME = "inventree_nfc_scanner"
TARGET = "esp32s3"
PROTO_VERSIONS = (1,)  # the scanner protocols this plugin speaks
APP_MAX = 0x1E0000  # an application slot
MERGED_MAX = 0x400000  # the whole flash
MANIFEST_MAX = 16 * 1024
CHECK_CACHE_KEY = "nfcscanner_release_check"
GITHUB_API = "https://api.github.com"

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class FirmwareError(Exception):
    """A release that cannot be taken: malformed, inconsistent, or not for this plugin."""


def parse_version(text: str):
    """A sort key for "1.2.3" or "1.2.3-rc.1": a pre-release sorts before its release."""
    m = VERSION_RE.match(text or "")
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    if pre is None:
        pre_key = (1,)
    else:
        pre_key = (0,) + tuple(
            (0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")
        )
    return (int(major), int(minor), int(patch), pre_key)


def newer(a: str, b: str) -> bool:
    """Whether version `a` is newer than `b`. An unreadable version is never newer."""
    ka, kb = parse_version(a), parse_version(b)
    if ka is None:
        return False
    if kb is None:
        return True
    return ka > kb


def newest(firmware) -> Firmware | None:
    """The newest of some Firmware rows, by version."""
    best = None
    for fw in firmware:
        if best is None or newer(fw.version, best.version):
            best = fw
    return best


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _asset(manifest: dict, key: str, limit: int, required: bool) -> dict | None:
    a = manifest.get(key)
    if a is None and not required:
        return None
    if not isinstance(a, dict):
        raise FirmwareError(f"manifest: {key} missing")
    file, size, digest, offset = (
        a.get("file"),
        a.get("size"),
        a.get("sha256"),
        a.get("offset"),
    )
    if not isinstance(file, str) or not FILE_RE.match(file):
        raise FirmwareError(f"manifest: {key}.file is not a plain file name")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= limit:
        raise FirmwareError(f"manifest: {key}.size must be 1 to {limit} bytes")
    if not isinstance(digest, str) or not SHA256_RE.match(digest):
        raise FirmwareError(f"manifest: {key}.sha256 must be 64 lower-case hex digits")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise FirmwareError(f"manifest: {key}.offset must be a whole number")
    return a


def check_manifest(manifest) -> dict:
    """The manifest, if it describes a release this plugin can hold. Raises FirmwareError."""
    if not isinstance(manifest, dict):
        raise FirmwareError("manifest: not an object")
    if manifest.get("name") != NAME:
        raise FirmwareError(f"manifest: not a {NAME} release")
    if manifest.get("target") != TARGET:
        raise FirmwareError(
            f"manifest: built for {manifest.get('target')!r}, not {TARGET}"
        )
    version = manifest.get("version")
    if not isinstance(version, str) or parse_version(version) is None:
        raise FirmwareError("manifest: version is not like 1.2.3 or 1.2.3-rc.1")
    for key in ("proto", "settings_version"):
        v = manifest.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise FirmwareError(f"manifest: {key} must be a whole number")
    if parse_version(str(manifest.get("min_plugin", ""))) is None:
        raise FirmwareError("manifest: min_plugin is not a version")
    _asset(manifest, "app", APP_MAX, required=True)
    _asset(manifest, "merged", MERGED_MAX, required=False)
    return manifest


def compatible(fw: Firmware) -> str | None:
    """Why this plugin cannot send this firmware to a scanner, or None if it can."""
    if fw.proto not in PROTO_VERSIONS:
        return f"it speaks scanner protocol {fw.proto}; this plugin speaks {', '.join(map(str, PROTO_VERSIONS))}"
    if newer(fw.min_plugin, PLUGIN_VERSION):
        return f"it needs plugin {fw.min_plugin} or newer; this is {PLUGIN_VERSION}"
    if not fw.available:
        return "its image has been pruned; upload it again"
    return None


def store(
    manifest,
    app: bytes,
    merged: bytes | None = None,
    *,
    source: str,
    user=None,
    release_url: str = "",
    published_at: datetime.datetime | None = None,
    prerelease: bool | None = None,
    digests: dict | None = None,
) -> Firmware:
    """Check a release's files against its manifest (and against GitHub's own digests, when
    given as {file name: hex}) and keep it. Raises FirmwareError."""
    manifest = check_manifest(manifest)
    version = manifest["version"]
    files = [("app", app)]
    if merged is not None and manifest.get("merged"):
        files.append(("merged", merged))
    for key, data in files:
        a = manifest[key]
        if len(data) != a["size"]:
            raise FirmwareError(
                f"{a['file']}: {len(data)} bytes, the manifest says {a['size']}"
            )
        got = sha256(data)
        if got != a["sha256"]:
            raise FirmwareError(
                f"{a['file']}: sha256 {got} does not match the manifest"
            )
        theirs = (digests or {}).get(a["file"])
        if theirs and theirs != got:
            raise FirmwareError(
                f"{a['file']}: sha256 {got} does not match GitHub's {theirs}"
            )

    existing = Firmware.objects.filter(version=version).first()
    if existing:
        if existing.app_sha256 != manifest["app"]["sha256"]:
            raise FirmwareError(
                f"{version} is held already with a different image; delete it first"
            )
        if not existing.available:  # pruned: put the image back
            existing.app.save(manifest["app"]["file"], ContentFile(app))
        return existing

    with transaction.atomic():
        fw = Firmware(
            version=version,
            prerelease="-" in version if prerelease is None else prerelease,
            source=source,
            release_url=release_url,
            published_at=published_at or timezone.now(),
            added_by=user,
            manifest=manifest,
            proto=manifest["proto"],
            settings_version=manifest["settings_version"],
            min_plugin=manifest["min_plugin"],
            app_size=manifest["app"]["size"],
            app_sha256=manifest["app"]["sha256"],
        )
        fw.app.save(manifest["app"]["file"], ContentFile(app), save=False)
        if len(files) > 1:
            fw.merged.save(manifest["merged"]["file"], ContentFile(merged), save=False)
            fw.merged_size = manifest["merged"]["size"]
            fw.merged_sha256 = manifest["merged"]["sha256"]
        fw.save()
    logger.info("NFC scanner firmware %s added (%s)", version, source)
    return fw


def remove_files(fw: Firmware) -> None:
    """Delete a release's images, keeping its record (deployments refer to it)."""
    for field in (fw.app, fw.merged):
        if field:
            field.delete(save=False)
    fw.save()


def prune(keep: int) -> list[str]:
    """Keep the images of the newest `keep` releases, and of any a deployment still needs;
    delete the rest. Returns the versions pruned."""
    if keep <= 0:
        return []
    held = [fw for fw in Firmware.objects.all() if fw.available]
    held.sort(key=lambda fw: parse_version(fw.version) or (), reverse=True)
    busy = set(
        Deployment.objects.exclude(state__in=Deployment.FINISHED).values_list(
            "firmware_id", flat=True
        )
    )
    pruned = []
    for fw in held[keep:]:
        if fw.pk not in busy:
            remove_files(fw)
            pruned.append(fw.version)
    return pruned


# GitHub


def _session(token: str):
    import requests

    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"inventree-nfc-scanner-plugin/{PLUGIN_VERSION}",
    })
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def _download(session, asset: dict, limit: int) -> bytes:
    """An asset's bytes, through the API (works for public and private repositories). The
    redirect to GitHub's storage drops the Authorization header by itself."""
    if asset.get("size", 0) > limit:
        raise FirmwareError(
            f"{asset.get('name')}: {asset.get('size')} bytes is too large"
        )
    r = session.get(
        asset["url"],
        headers={"Accept": "application/octet-stream"},
        stream=True,
        timeout=30,
    )
    r.raise_for_status()
    data = bytearray()
    for chunk in r.iter_content(65536):
        data += chunk
        if len(data) > limit:
            raise FirmwareError(f"{asset.get('name')}: larger than {limit} bytes")
    return bytes(data)


def _github_digest(asset: dict) -> str | None:
    d = asset.get("digest") or ""
    return d.split(":", 1)[1] if d.startswith("sha256:") else None


def fetch_github(
    repo: str,
    *,
    token: str = "",
    include_prereleases: bool = False,
    api: str = GITHUB_API,
    limit: int = 10,
) -> dict:
    """Store every release of `repo` not held yet. Returns {"added": [...], "errors": [...]}.
    A release that fails its checks is reported and skipped; the others are still taken."""
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo or ""):
        raise FirmwareError(f"{repo!r} is not owner/name")
    session = _session(token)
    r = session.get(
        f"{api.rstrip('/')}/repos/{repo}/releases",
        params={"per_page": limit},
        timeout=30,
    )
    if r.status_code == 404:
        raise FirmwareError(
            f"{repo}: no such repository, or it is private and no token is set"
        )
    if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
        raise FirmwareError("GitHub's rate limit is used up; try later, or set a token")
    r.raise_for_status()

    # Every version with a record, pruned ones included, so pruning is not undone by the
    # next check. A pruned image comes back by uploading it.
    held = set(Firmware.objects.values_list("version", flat=True))
    added, errors = [], []
    for rel in r.json():
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        if rel.get("prerelease") and not include_prereleases:
            continue
        tag = str(rel.get("tag_name") or "")
        version = tag[1:] if tag.startswith("v") else tag
        if version in held or parse_version(version) is None:
            continue
        try:
            assets = {a.get("name"): a for a in rel.get("assets") or []}
            if "manifest.json" not in assets:
                raise FirmwareError("no manifest.json in the release")
            import json

            manifest = check_manifest(
                json.loads(_download(session, assets["manifest.json"], MANIFEST_MAX))
            )
            if manifest["version"] != version:
                raise FirmwareError(
                    f"the manifest is for {manifest['version']}, the tag is {tag}"
                )
            app_name = manifest["app"]["file"]
            if app_name not in assets:
                raise FirmwareError(f"{app_name} is not in the release")
            app = _download(session, assets[app_name], APP_MAX)
            merged = None
            merged_name = (manifest.get("merged") or {}).get("file")
            if merged_name in assets:
                merged = _download(session, assets[merged_name], MERGED_MAX)
            digests = {
                name: _github_digest(a)
                for name, a in assets.items()
                if _github_digest(a)
            }
            published = rel.get("published_at")
            store(
                manifest,
                app,
                merged,
                source=Firmware.Source.GITHUB,
                release_url=str(rel.get("html_url") or "")[:300],
                published_at=datetime.datetime.fromisoformat(
                    published.replace("Z", "+00:00")
                )
                if published
                else None,
                prerelease=bool(rel.get("prerelease")),
                digests=digests,
            )
            added.append(version)
        except Exception as exc:  # one bad release must not stop the others
            logger.warning("NFC scanner firmware %s not taken: %s", tag, exc)
            errors.append(f"{tag}: {exc}")
    return {"added": added, "errors": errors}


def last_check() -> dict | None:
    """When the releases were last checked, and what came of it."""
    return cache.get(CHECK_CACHE_KEY)


def record_check(result: dict) -> dict:
    result = dict(result, at=timezone.now().isoformat())
    cache.set(CHECK_CACHE_KEY, result, timeout=None)
    return result
