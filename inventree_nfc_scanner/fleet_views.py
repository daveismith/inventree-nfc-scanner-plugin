"""Endpoints for fleet updates.

- `api/fleet/...`: the admin's: the scanners and what they run, the firmware held, fetching
  and uploading releases, deploying. Admins only: superusers, and users whose group has
  change permission on InvenTree's admin role (which covers the machine configuration).
- `api/usb/...`: the browser's, when it connects to a scanner over USB: whether an update is
  waiting for that scanner, and the update's progress. Any user who may program tags, since
  that is who has the scanner plugged in; each answers only about the scanner in hand.
- `firmware/<version>/<file>`: the images, for scanners and browsers. Any signed-in user or
  token: what is served is public on GitHub already, and the scanner checks the sha256 it was
  given over its authenticated link before it installs anything.
"""

from __future__ import annotations

import json

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime

from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from . import fleet
from . import firmware as fwlib
from .models import Deployment, Firmware, Scanner
from .views import CanProgramTags


def is_fleet_admin(user) -> bool:
    """Who may see and manage fleet updates."""
    return bool(
        user
        and user.is_authenticated
        and (user.is_superuser or user.has_perm("machine.change_machineconfig"))
    )


class IsFleetAdmin(permissions.BasePermission):
    """Superusers, and the admin role with change permission."""

    def has_permission(self, request, view):
        """Check it."""
        return is_fleet_admin(request.user)


def firmware_dict(fw: Firmware) -> dict:
    """A release as the fleet page shows it."""
    return {
        "id": fw.pk,
        "version": fw.version,
        "prerelease": fw.prerelease,
        "source": fw.source,
        "release_url": fw.release_url,
        "published_at": fw.published_at.isoformat() if fw.published_at else None,
        "added_at": fw.added_at.isoformat(),
        "available": fw.available,
        "size": fw.app_size,
        "sha256": fw.app_sha256,
        "proto": fw.proto,
        "settings_version": fw.settings_version,
        "min_plugin": fw.min_plugin,
        "git_sha": str(fw.manifest.get("git_sha", ""))[:40],
        "merged": bool(fw.merged),
        "incompatible": fwlib.compatible(fw),
    }


def deployment_dict(dep: Deployment) -> dict:
    """A deployment as the fleet page shows it."""
    return {
        "id": dep.pk,
        "reader": dep.scanner.reader_id,
        "version": dep.firmware.version,
        "state": dep.state,
        "via": dep.via or None,
        "from_version": dep.from_version or None,
        "required": fleet.is_required(dep),
        "required_flag": dep.required,
        "required_after": dep.required_after.isoformat()
        if dep.required_after
        else None,
        "requested_by": dep.requested_by.username if dep.requested_by else None,
        "requested_at": dep.requested_at.isoformat(),
        "started_at": dep.started_at.isoformat() if dep.started_at else None,
        "finished_at": dep.finished_at.isoformat() if dep.finished_at else None,
        "attempts": dep.attempts,
        "deferrals": dep.deferrals,
        "error": dep.error,
        "detail": dep.detail,
    }


def scanner_dict(scanner: Scanner, newest: Firmware | None) -> dict:
    """A scanner as the fleet page shows it."""
    machine = fleet.network_machine(scanner.reader_id)
    current = (
        Deployment.objects.filter(scanner=scanner)
        .exclude(state__in=Deployment.FINISHED)
        .select_related("firmware", "requested_by", "scanner")
        .first()
    )
    return {
        "reader": scanner.reader_id,
        "fw": scanner.fw or None,
        "proto": scanner.proto,
        "last_seen": scanner.last_seen.isoformat() if scanner.last_seen else None,
        "last_via": scanner.last_via or None,
        "last_user": scanner.last_user.username if scanner.last_user else None,
        "machine": {"id": str(machine.pk), "name": machine.name} if machine else None,
        "outdated": bool(
            newest and scanner.fw and fwlib.newer(newest.version, scanner.fw)
        ),
        "deployment": deployment_dict(current) if current else None,
    }


class FleetView(APIView):
    """GET api/fleet/ : every scanner, the firmware held, and the last release check."""

    permission_classes = [IsFleetAdmin]

    def get(self, request, *args, **kwargs):
        """The overview."""
        held = list(Firmware.objects.all())
        newest = fleet.newest_firmware(
            include_prereleases=bool(fleet.setting("FIRMWARE_PRERELEASES", False))
        )
        return Response({
            "scanners": [scanner_dict(s, newest) for s in Scanner.objects.all()],
            "firmware": [
                firmware_dict(fw)
                for fw in sorted(
                    held,
                    key=lambda fw: fwlib.parse_version(fw.version) or (),
                    reverse=True,
                )
            ],
            "newest": newest.version if newest else None,
            "last_check": fwlib.last_check(),
            "repo": fleet.setting("FIRMWARE_REPO", ""),
            "policy": fleet.setting("FIRMWARE_USB_POLICY", fleet.POLICY_DEFERRABLE),
        })


def run_check() -> dict:
    """Fetch new releases from GitHub, then prune and deploy as the settings say."""
    repo = fleet.setting("FIRMWARE_REPO", "")
    if not repo:
        return fwlib.record_check({"added": [], "errors": ["no repository is set"]})
    try:
        result = fwlib.fetch_github(
            repo,
            token=str(fleet.setting("FIRMWARE_GITHUB_TOKEN", "") or "").strip(),
            include_prereleases=bool(fleet.setting("FIRMWARE_PRERELEASES", False)),
            api=str(fleet.setting("FIRMWARE_API", fwlib.GITHUB_API)),
        )
    except Exception as exc:  # noqa: BLE001 - reported to the admin, not raised
        result = {"added": [], "errors": [str(exc)[:300]]}
    # What follows must not lose the check: whatever goes wrong is recorded with it.
    try:
        result["pruned"] = fwlib.prune(int(fleet.setting("FIRMWARE_KEEP", 5)))
    except Exception as exc:  # noqa: BLE001 - reported to the admin, not raised
        result["pruned"] = []
        result["errors"].append(f"pruning: {exc}"[:300])
    if result["added"]:
        try:
            deployed = fleet.auto_deploy(result["added"])
        except Exception as exc:  # noqa: BLE001 - reported to the admin, not raised
            deployed = None
            result["errors"].append(f"deploying automatically: {exc}"[:300])
        if deployed:
            result["auto_deploy"] = deployed
            if deployed.get("refused"):
                result["errors"].append(
                    f"{deployed['version']} not deployed automatically: {deployed['refused']}"[
                        :300
                    ]
                )
    return fwlib.record_check(result)


class FleetCheckView(APIView):
    """POST api/fleet/check/ : look for new releases now."""

    permission_classes = [IsFleetAdmin]

    def post(self, request, *args, **kwargs):
        """Check."""
        return Response(run_check())


class FirmwareUploadView(APIView):
    """POST api/fleet/upload/ : a release's files, for a server without internet access.

    Multipart: `manifest` (manifest.json), `app` (the app image), and optionally `merged`.
    """

    permission_classes = [IsFleetAdmin]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        """Check and keep it."""
        files = request.FILES
        if "manifest" not in files or "app" not in files:
            raise ValidationError({"detail": "manifest and app are required"})
        try:
            manifest = json.loads(files["manifest"].read(fwlib.MANIFEST_MAX + 1))
        except ValueError:
            raise ValidationError({"manifest": "not JSON"})

        def read(name, limit):
            if name not in files:
                return None
            if files[name].size > limit:
                raise ValidationError({name: f"larger than {limit} bytes"})
            return files[name].read()

        try:
            fw = fwlib.store(
                manifest,
                read("app", fwlib.APP_MAX),
                read("merged", fwlib.MERGED_MAX),
                source=Firmware.Source.UPLOAD,
                user=request.user,
            )
        except fwlib.FirmwareError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(firmware_dict(fw), status=status.HTTP_201_CREATED)


class FirmwareDetailView(APIView):
    """DELETE api/fleet/firmware/<pk>/ : remove a release's images, as pruning does.

    The record of a release from GitHub is kept, so that the next check does not fetch it
    again (it is held, without images); so is that of any release a deployment's history
    names. An uploaded release nothing refers to goes entirely. Uploading a release again
    brings back its images either way. Refused while a deployment of it is unfinished."""

    permission_classes = [IsFleetAdmin]

    def delete(self, request, pk, *args, **kwargs):
        """Remove it."""
        fw = get_object_or_404(Firmware, pk=pk)
        if (
            Deployment.objects.filter(firmware=fw)
            .exclude(state__in=Deployment.FINISHED)
            .exists()
        ):
            raise ValidationError({
                "detail": "a deployment of it is not finished; cancel it first"
            })
        fwlib.remove_files(fw)
        if fw.source == Firmware.Source.UPLOAD and not fw.deployments.exists():
            fw.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(firmware_dict(fw))


class ScannerDetailView(APIView):
    """DELETE api/fleet/scanners/<reader>/ : forget a scanner (one taken out of service), with
    its deployment history. It comes back by itself if it is heard from again."""

    permission_classes = [IsFleetAdmin]

    def delete(self, request, reader, *args, **kwargs):
        """Forget it."""
        scanner = get_object_or_404(Scanner, reader_id=reader)
        if scanner.deployments.filter(state__in=Deployment.IN_FLIGHT).exists():
            raise ValidationError({"detail": "it is in the middle of an update"})
        scanner.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeployView(APIView):
    """POST api/fleet/deploy/ : tell scanners to run a firmware.

    {"firmware": <id>, "scanners": ["nfc-…", …] or "all", "required": true|false|null,
     "required_after": "<ISO 8601>"|null, "allow_downgrade": false}
    """

    permission_classes = [IsFleetAdmin]
    parser_classes = [JSONParser]

    def post(self, request, *args, **kwargs):
        """Deploy."""
        d = request.data if isinstance(request.data, dict) else {}
        fw = get_object_or_404(Firmware, pk=d.get("firmware"))
        wanted = d.get("scanners")
        if wanted == "all":
            scanners = list(Scanner.objects.all())
        elif isinstance(wanted, list) and all(isinstance(r, str) for r in wanted):
            scanners = list(Scanner.objects.filter(reader_id__in=wanted))
            missing = set(wanted) - {s.reader_id for s in scanners}
            if missing:
                raise ValidationError({
                    "scanners": f"unknown: {', '.join(sorted(missing))}"
                })
        else:
            raise ValidationError({"scanners": 'a list of reader ids, or "all"'})
        required = d.get("required")
        if required is not None and not isinstance(required, bool):
            raise ValidationError({"required": "true, false or null"})
        required_after = None
        if d.get("required_after"):
            required_after = parse_datetime(str(d["required_after"]))
            if required_after is None:
                raise ValidationError({"required_after": "not a date and time"})
        try:
            results = fleet.deploy(
                fw,
                scanners,
                user=request.user,
                required=required,
                required_after=required_after,
                allow_downgrade=d.get("allow_downgrade") is True,
            )
        except fleet.Refused as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"results": results})


class DeploymentListView(APIView):
    """GET api/fleet/deployments/?scanner=<reader id> : the history, newest first (100)."""

    permission_classes = [IsFleetAdmin]

    def get(self, request, *args, **kwargs):
        """List."""
        deps = Deployment.objects.select_related("scanner", "firmware", "requested_by")
        if request.query_params.get("scanner"):
            deps = deps.filter(scanner__reader_id=request.query_params["scanner"])
        return Response([deployment_dict(d) for d in deps[:100]])


class DeploymentCancelView(APIView):
    """POST api/fleet/deployments/<pk>/cancel/ : withdraw one not yet started."""

    permission_classes = [IsFleetAdmin]

    def post(self, request, pk, *args, **kwargs):
        """Cancel."""
        dep = get_object_or_404(Deployment, pk=pk)
        try:
            dep = fleet.cancel(dep)
        except fleet.Refused as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(deployment_dict(dep))


# The browser's side


def _reader(data) -> str:
    reader = data.get("reader") if isinstance(data, dict) else None
    if not isinstance(reader, str) or not reader.startswith("nfc-") or len(reader) > 32:
        raise ValidationError({"reader": "the scanner's reader id, from its hello"})
    return reader


def offer_dict(dep: Deployment) -> dict:
    """An update waiting for the scanner in hand, as the browser needs it."""
    return {
        "id": dep.pk,
        "version": dep.firmware.version,
        "required": fleet.is_required(dep),
        "required_after": dep.required_after.isoformat()
        if dep.required_after
        else None,
        "size": dep.firmware.app_size,
        "sha256": dep.firmware.app_sha256,
        "url": fleet.image_path(dep.firmware),
        "deferrals": dep.deferrals,
    }


class UsbCheckinView(APIView):
    """POST api/usb/checkin/ {"reader", "fw", "proto"} : a browser has connected to a scanner.

    Records it, settles an update it was in the middle of, and says whether one is waiting.
    """

    permission_classes = [CanProgramTags]

    def post(self, request, *args, **kwargs):
        """Check in."""
        reader = _reader(request.data)
        fw = request.data.get("fw")
        scanner = fleet.note_scanner(
            reader,
            fw=fw if isinstance(fw, str) else "",
            proto=request.data.get("proto"),
            via=Scanner.Via.USB,
            user=request.user,
        )
        dep = fleet.usb_offer(scanner)
        last = (
            Deployment.objects.filter(scanner=scanner, via=Scanner.Via.USB)
            .exclude(state=Deployment.State.PENDING)
            .select_related("firmware")
            .first()
        )
        return Response({
            "reader": scanner.reader_id,
            "update": offer_dict(dep) if dep else None,
            # The outcome of the last update over USB, so the browser can say how it went.
            "last": {
                "id": last.pk,
                "version": last.firmware.version,
                "state": last.state,
                "error": last.error,
                "detail": last.detail,
            }
            if last
            else None,
        })


def _usb_deployment(request, pk) -> Deployment:
    reader = _reader(request.data)
    dep = get_object_or_404(
        Deployment.objects.select_related("scanner", "firmware"), pk=pk
    )
    if dep.scanner.reader_id != reader:
        raise PermissionDenied("that update is for another scanner")
    return dep


class UsbStartView(APIView):
    """POST api/usb/deployments/<pk>/start/ {"reader"} : the browser is about to install it."""

    permission_classes = [CanProgramTags]

    def post(self, request, pk, *args, **kwargs):
        """Claim it."""
        dep = _usb_deployment(request, pk)
        try:
            dep = fleet.usb_claim(dep, request.user)
        except fleet.Refused as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(offer_dict(dep))


class UsbDeferView(APIView):
    """POST api/usb/deployments/<pk>/defer/ {"reader"} : the user chose "later"."""

    permission_classes = [CanProgramTags]

    def post(self, request, pk, *args, **kwargs):
        """Put it off, if the policy allows."""
        dep = _usb_deployment(request, pk)
        try:
            dep = fleet.usb_defer(dep)
        except fleet.Refused as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(offer_dict(dep))


class UsbReportView(APIView):
    """POST api/usb/deployments/<pk>/report/ {"reader", "state", "error", "detail"} :
    progress of an update the browser is installing: downloading, restarting or failed."""

    permission_classes = [CanProgramTags]

    def post(self, request, pk, *args, **kwargs):
        """Record it."""
        dep = _usb_deployment(request, pk)
        state = request.data.get("state")
        if state not in ("downloading", "restarting", "failed"):
            raise ValidationError({"state": "downloading, restarting or failed"})
        error = request.data.get("error")
        detail = request.data.get("detail")
        try:
            dep = fleet.usb_report(
                dep,
                state,
                error if isinstance(error, str) else "",
                detail if isinstance(detail, str) else "",
            )
        except fleet.Refused as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"id": dep.pk, "state": dep.state})


class FirmwareImageView(APIView):
    """GET firmware/<version>/<file> : an image, for a scanner or a browser installing it."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, version, name, *args, **kwargs):
        """Stream it."""
        fw = Firmware.objects.filter(version=version).first()
        if fw is None:
            raise Http404
        for field, key in ((fw.app, "app"), (fw.merged, "merged")):
            if field and fw.manifest.get(key, {}).get("file") == name:
                response = FileResponse(
                    field.open("rb"),
                    content_type="application/octet-stream",
                    as_attachment=True,
                    filename=name,
                )
                response["Content-Length"] = field.size
                response["ETag"] = f'"{fw.manifest[key]["sha256"]}"'
                response["Cache-Control"] = "private, max-age=86400, immutable"
                return response
        raise Http404
