"""The plugin's HTTP endpoints: /sync for scanners, and a small API for the panel."""

import logging
import uuid

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from machine import registry
from stock.models import StockLocation

from . import ndef
from .machine import NETWORK_DRIVER, NfcScannerMachine, NfcScannerStatus
from .models import Job, ScannerCommand
from .serializers import JobCreateSerializer, JobSerializer, UsbJobSerializer
from .sync import enqueue, handle_sync, retire, shared_cache

logger = logging.getLogger("inventree")


def plugin():
    """The running plugin instance (for its settings)."""
    from plugin import registry as plugin_registry

    return plugin_registry.get_plugin("nfcscanner")


def base_url() -> str:
    """The server's own address, which goes into every tag's URI record."""
    from common.settings import get_global_setting

    url = get_global_setting("INVENTREE_BASE_URL", "") or ""
    if not url:
        raise ValidationError({
            "base_url": 'Set the "Base URL" global setting first; it goes on every tag.'
        })
    return url


class CanProgramTags(permissions.BasePermission):
    """Programming a tag changes the location's barcode, so it needs that permission."""

    def has_permission(self, request, view):
        """Check the stock location change permission."""
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("stock.change_stocklocation")
        )


class CanSeeScanners(permissions.BasePermission):
    """Reading jobs and scanners: what a tag holds is stock information."""

    def has_permission(self, request, view):
        """Check the stock location view permission."""
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("stock.view_stocklocation")
        )


def tag_payload(location: StockLocation) -> dict:
    """What a `program` job for this location carries."""
    plg = plugin()
    data = {
        "location": location.pk,
        "text": ndef.location_text(location.pk),
        "uri": ndef.location_uri(base_url(), location.pk),
        "ndef": ndef.build_message(base_url(), location.pk).hex().upper(),
        "timeout_s": int(plg.get_setting("JOB_TIMEOUT_S") or 60),
    }
    pwd = (plg.get_setting("TAG_PASSWORD") or "").strip().upper()
    if pwd:
        data["pwd"] = pwd
        data["pack"] = (plg.get_setting("TAG_PACK") or "0000").strip().upper()
    return data


class LocationTagView(APIView):
    """GET api/location/<pk>/tag/ : the message for the location's tag, for the USB route."""

    permission_classes = [CanProgramTags]

    def get(self, request, pk, *args, **kwargs):
        """Build it."""
        location = get_object_or_404(StockLocation, pk=pk)
        return Response(tag_payload(location))


class LinkView(APIView):
    """POST api/location/<pk>/link/ : make a tag's UID the location's barcode.

    Unlike InvenTree's own barcode link, this takes the barcode away from whatever held it
    before (a bin the tag used to belong to), which is what re-programming a tag means.
    """

    permission_classes = [CanProgramTags]

    def post(self, request, pk, *args, **kwargs):
        """Link it."""
        from .barcodes import BadUid, NotPermitted, clean_uid, link_uid

        location = get_object_or_404(StockLocation, pk=pk)
        if not isinstance(request.data, dict):
            raise ValidationError({"uid": "expected an object with a uid"})
        try:
            uid = clean_uid(request.data.get("uid", ""))
            outcome = link_uid(location, uid, request.user)
        except BadUid as exc:
            raise ValidationError({"uid": str(exc)})
        except NotPermitted as exc:
            raise PermissionDenied(str(exc))
        except Exception:
            logger.exception(
                "NFC: could not link %s to location %s for %s",
                request.data.get("uid") if isinstance(request.data, dict) else None,
                pk,
                request.user,
            )
            raise ValidationError({
                "uid": "the barcode could not be linked; the server log has the reason"
            })
        return Response({"location": location.pk, "uid": uid, "outcome": outcome})


class UsbJobView(APIView):
    """POST api/location/<pk>/jobs/usb/ : record the outcome of a job the browser did over USB."""

    permission_classes = [CanProgramTags]

    def post(self, request, pk, *args, **kwargs):
        """Record it."""
        location = get_object_or_404(StockLocation, pk=pk)
        serializer = UsbJobSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        job = Job.objects.create(
            kind=d.get("kind", Job.Kind.PROGRAM),
            location=location,
            machine=None,
            created_by=request.user,
            state=d["state"],
            overwrite=d.get("overwrite", False),
            uid=d.get("uid", ""),
            tag_type=d.get("tag_type", ""),
            protected=d.get("protected"),
            error=d.get("error", ""),
            error_detail=d.get("error_detail", ""),
            finished_at=timezone.now(),
        )
        return Response(JobSerializer(job).data, status=status.HTTP_201_CREATED)


def scanner_machines() -> list[NfcScannerMachine]:
    """Every active NFC scanner machine."""
    return [
        m
        for m in registry.get_machines(active=True)
        if isinstance(m, NfcScannerMachine)
    ]


def scanner_dict(machine: NfcScannerMachine) -> dict:
    """A scanner as the panel sees it."""
    seen = machine.last_seen
    driver = registry.get_driver_instance(NETWORK_DRIVER)
    shared = (
        driver.shares_user_with(machine)
        if driver and machine.machine_config.driver == NETWORK_DRIVER
        else []
    )
    return {
        "warning": f"shares its user with {', '.join(shared)}; give each scanner a user of its own"
        if shared
        else None,
        "id": str(machine.pk),
        "name": machine.name,
        # So a browser can tell which of these is the scanner plugged into it.
        "reader": machine.get_setting("READER_ID", "D") or None
        if machine.machine_config.driver == NETWORK_DRIVER
        else None,
        "driver": machine.machine_config.driver,
        "status": machine.status.name.lower(),
        "status_text": machine.status_text,
        "online": machine.status in (NfcScannerStatus.ONLINE, NfcScannerStatus.BUSY),
        "last_seen": seen.isoformat() if seen else None,
        "last_tag": machine.last_tag,
        "location": machine.get_setting("LOCATION", "M") or None,
    }


class ScannerListView(APIView):
    """GET api/scanners/ : the scanners, with their state."""

    permission_classes = [CanSeeScanners]

    def get(self, request, *args, **kwargs):
        """List them."""
        return Response([scanner_dict(m) for m in scanner_machines()])


class JobListView(APIView):
    """GET api/jobs/ : recent jobs. POST api/jobs/ : a job for a network scanner."""

    def get_permissions(self):
        """Reading needs view permission; queuing a job needs change permission."""
        return (
            [CanSeeScanners()] if self.request.method == "GET" else [CanProgramTags()]
        )

    def get(self, request, *args, **kwargs):
        """Recent jobs, newest first; `location` and `scanner` filter."""
        jobs = Job.objects.all()
        if request.query_params.get("location"):
            try:
                jobs = jobs.filter(location_id=int(request.query_params["location"]))
            except ValueError:
                raise ValidationError({"location": "expected an integer"})
        if request.query_params.get("scanner"):
            try:
                jobs = jobs.filter(
                    machine_id=uuid.UUID(request.query_params["scanner"])
                )
            except ValueError:
                raise ValidationError({"scanner": "expected a machine id"})
        return Response(JobSerializer(jobs[:50], many=True).data)

    def post(self, request, *args, **kwargs):
        """Queue a job for a network scanner."""
        serializer = JobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        location = get_object_or_404(StockLocation, pk=d["location"])

        machine = registry.get_machine(d["scanner"])
        if (
            not isinstance(machine, NfcScannerMachine)
            or not machine.active
            or not machine.initialized
        ):
            raise ValidationError({"scanner": "No such scanner, or it is not active."})
        if machine.machine_config.driver != NETWORK_DRIVER:
            raise ValidationError({"scanner": "That scanner is not a network scanner."})
        # Status lives in the cache. With the shared cache it is the truth from every
        # process; with the per-process fallback each worker has its own idea of it, and a
        # refusal here would be a lottery, so the gate is only applied when it can be right.
        if shared_cache() and machine.status == NfcScannerStatus.OFFLINE:
            raise ValidationError({
                "scanner": "That scanner is offline; a job for it would only wait."
            })

        from .models import Deployment

        if Deployment.objects.filter(
            scanner__reader_id=machine.get_setting("READER_ID", "D"),
            via="network",
            state__in=Deployment.IN_FLIGHT,
        ).exists():
            raise ValidationError({
                "scanner": "That scanner is updating its firmware; try again in a minute."
            })

        kind = d.get("kind", Job.Kind.PROGRAM)
        timeout_s = int(plugin().get_setting("JOB_TIMEOUT_S") or 60)
        # The command's contents are built before anything is written, so that a missing base
        # URL (a 400 from tag_payload) leaves no job behind.
        if kind == Job.Kind.PROGRAM:
            payload = tag_payload(location)
            cmd = {
                "cmd": "program",
                "ndef": payload["ndef"],
                "timeout_ms": timeout_s * 1000,
            }
            if d.get("overwrite", False):
                cmd["overwrite"] = True
            if "pwd" in payload:
                cmd["pwd"] = payload["pwd"]
                cmd["pack"] = payload["pack"]
        else:
            cmd = {"cmd": "wipe", "timeout_ms": timeout_s * 1000}
            pwd = (plugin().get_setting("TAG_PASSWORD") or "").strip().upper()
            if pwd:
                cmd["pwd"] = pwd

        with transaction.atomic():
            job = Job.objects.create(
                kind=kind,
                location=location,
                machine=machine.machine_config,
                created_by=request.user,
                overwrite=d.get("overwrite", False),
                timeout_s=timeout_s,
            )
            enqueue(machine.machine_config, dict(cmd, id=job.pk), job=job)
        return Response(JobSerializer(job).data, status=status.HTTP_201_CREATED)


class JobDetailView(APIView):
    """GET api/jobs/<pk>/ : one job."""

    permission_classes = [CanSeeScanners]

    def get(self, request, pk, *args, **kwargs):
        """Return it."""
        return Response(JobSerializer(get_object_or_404(Job, pk=pk)).data)


class JobCancelView(APIView):
    """POST api/jobs/<pk>/cancel/ : cancel a job that has not finished."""

    permission_classes = [CanProgramTags]

    def post(self, request, pk, *args, **kwargs):
        """Cancel it: at once if the scanner has not collected it, else by asking the scanner."""
        with transaction.atomic():
            # The job row first, then its commands: the same order as the sync handing them
            # out, so the two cannot wait on each other.
            job = get_object_or_404(Job.objects.select_for_update(), pk=pk)
            if job.finished:
                return Response(JobSerializer(job).data)
            unsent = ScannerCommand.objects.select_for_update().filter(
                job=job, acked_at__isnull=True, sent_at__isnull=True
            )
            machine_gone = job.machine is None or not job.machine.active
            if machine_gone or (job.state == Job.State.QUEUED and unsent.exists()):
                unsent.delete()
                job.state = Job.State.CANCELLED
                job.finished_at = timezone.now()
                job.save()
                # Anything already sent is done with too, its secrets included.
                retire(ScannerCommand.objects.filter(job=job, acked_at__isnull=True))
            elif not ScannerCommand.objects.filter(
                job=job, acked_at__isnull=True, payload__cmd="cancel"
            ).exists():
                enqueue(job.machine, {"cmd": "cancel", "id": job.pk}, job=job)
        return Response(JobSerializer(job).data)


class SyncView(APIView):
    """POST sync/ : the exchange with a network scanner. Token authentication."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        """Handle one call."""
        body = request.data if isinstance(request.data, dict) else {}
        reader_id = str(body.get("reader", "")).strip()
        if not reader_id:
            raise ValidationError({"reader": "required"})
        if body.get("proto") != 1:
            raise ValidationError({"proto": "this plugin speaks protocol version 1"})

        driver = registry.get_driver_instance(NETWORK_DRIVER)
        machine = driver.find_by_reader_id(reader_id) if driver else None
        # An unknown reader and a reader that is not this token's get the same answer, so a
        # token cannot be used to find out which reader ids exist.
        if machine is None or str(machine.get_setting("USER", "D") or "") != str(
            request.user.pk
        ):
            shown = "".join(c for c in reader_id[:40] if c.isprintable())
            logger.info(
                "NFC /sync refused for reader %r (user %s)", shown, request.user
            )
            raise PermissionDenied(
                "No scanner with that reader id is configured for this token."
            )

        plg = plugin()
        long_poll = bool(plg.get_setting("LONG_POLL")) if plg else False
        max_s = (
            int(plg.get_setting("LONG_POLL_MAX_S") or 0) if (plg and long_poll) else 0
        )
        origin = request.build_absolute_uri("/").rstrip("/")
        return Response(
            handle_sync(machine, body, long_poll_max_s=max_s, origin=origin)
        )
