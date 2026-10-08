"""Fleet updates: which scanners exist, what they run, and getting a firmware onto them.

A Deployment is one scanner told to run one firmware. It waits (pending) until the scanner
can be reached: a network scanner on its next call, a USB scanner when a browser next
connects to it, whichever comes first. Then:

    pending -> sent -> downloading -> restarting -> confirmed
                 \\-> failed      \\-> failed     \\-> rolled_back / failed

- Network: the plugin queues the scanner's `ota` command with the image's URL on this server
  and its sha256; the scanner's `rsp` and `ota` events move the row; its next call after a
  restart (a new boot) says what it now runs. Its own version is the verdict: the target
  means confirmed, the old one after a restart means the bootloader rolled it back.
- USB: the browser asks on connecting (a check-in), claims the deployment, streams the image
  over serial, and reports progress; its next check-in after the restart is the verdict.

An attempt interrupted before the scanner restarted (it lost power, the browser closed, a
busy scanner) goes back to pending, up to MAX_ATTEMPTS.
"""

from __future__ import annotations

import datetime
import logging

from django.db import transaction
from django.utils import timezone

from . import firmware as fwlib
from .models import Deployment, Firmware, Job, Scanner

logger = logging.getLogger("inventree")

MAX_ATTEMPTS = 3
BUSY_RETRY = datetime.timedelta(seconds=60)
SENT_TIMEOUT = datetime.timedelta(minutes=2)  # network: the command not even answered
DOWNLOAD_TIMEOUT = datetime.timedelta(minutes=10)
# Restarting, and not heard from since. Longer than the firmware's own deadline for a new
# image to prove itself (15 minutes, then the bootloader returns to the old one), so that a
# rollback is seen as one rather than as a scanner that never came back.
RESTART_TIMEOUT = datetime.timedelta(minutes=20)
USB_TIMEOUT = datetime.timedelta(minutes=15)  # a browser that went away mid-update

POLICY_REQUIRED = "required"
POLICY_DEFERRABLE = "deferrable"


class Refused(Exception):
    """A deployment that may not be made."""


def plugin():
    from plugin import registry as plugin_registry

    return plugin_registry.get_plugin("nfcscanner")


def setting(key, default=None):
    plg = plugin()
    value = plg.get_setting(key) if plg else None
    return default if value in (None, "") else value


def network_machine(reader_id: str):
    """The active network machine configured with this reader id, if any."""
    from machine import registry

    from .machine import NETWORK_DRIVER

    driver = registry.get_driver_instance(NETWORK_DRIVER)
    return driver.find_by_reader_id(reader_id) if driver else None


# The registry


def note_scanner(
    reader_id: str, *, fw: str, proto, boot=None, via: str, user=None
) -> Scanner:
    """Record that a scanner was heard from, and settle any update it was in the middle of."""
    reader_id = reader_id[:32]
    fw = fw[:40] if isinstance(fw, str) else ""
    with transaction.atomic():
        scanner, _ = Scanner.objects.select_for_update().get_or_create(
            reader_id=reader_id
        )
        restarted = (
            boot is not None and scanner.boot is not None and boot != scanner.boot
        )
        scanner.fw = fw or scanner.fw
        scanner.proto = proto if isinstance(proto, int) else scanner.proto
        if boot is not None:
            scanner.boot = boot
        scanner.last_seen = timezone.now()
        scanner.last_via = via
        if user is not None and user.is_authenticated:
            scanner.last_user = user
        scanner.save()
        _settle(scanner, via=via, restarted=restarted)
    scanner.restarted = restarted  # for the caller; not a field
    return scanner


def _end(dep: Deployment, state: str, error: str = "", detail: str = "") -> None:
    dep.state = state
    dep.error = error[:32]
    dep.detail = detail[:200]
    dep.finished_at = timezone.now()
    dep.save()
    if dep.via == Scanner.Via.NETWORK:
        _retire_ota(dep)
    if state in (Deployment.State.FAILED, Deployment.State.ROLLED_BACK):
        logger.warning(
            "NFC scanner %s: firmware %s %s (%s %s)",
            dep.scanner.reader_id,
            dep.firmware.version,
            state,
            error,
            detail,
        )
        _notify(dep)


def _retry_or_fail(dep: Deployment, error: str, detail: str = "", after=None) -> None:
    """An attempt that did not get as far as a restart: try again later, or give up."""
    if dep.attempts >= MAX_ATTEMPTS:
        _end(
            dep,
            Deployment.State.FAILED,
            error,
            f"{detail} (after {dep.attempts} attempts)".strip(),
        )
        return
    if dep.via == Scanner.Via.NETWORK:
        _retire_ota(dep)
    dep.state = Deployment.State.PENDING
    dep.error = error[:32]
    dep.detail = detail[:200]
    dep.retry_after = after
    dep.save()


def _settle(scanner: Scanner, *, via: str, restarted: bool) -> None:
    """What the scanner now reports says how its update went."""
    dep = (
        Deployment.objects.select_for_update()
        .filter(scanner=scanner, state__in=Deployment.IN_FLIGHT)
        .select_related("firmware")
        .first()
    )
    if dep is None:
        return
    target = dep.firmware.version
    if dep.via == Scanner.Via.NETWORK and via == Scanner.Via.NETWORK:
        if not restarted:
            return
    elif dep.via == Scanner.Via.USB and via == Scanner.Via.USB:
        # The browser checks in on every connection; one that comes back mid-transfer has
        # lost it (no restart has happened), one after `restarting` has the verdict.
        if dep.state != Deployment.State.RESTARTING and scanner.fw != target:
            _retry_or_fail(
                dep, "interrupted", "the connection was lost before the update finished"
            )
            return
    elif scanner.fw != target or dep.state != Deployment.State.RESTARTING:
        return  # heard from on the other link mid-update: wait for the one doing it

    if scanner.fw == target:
        _end(dep, Deployment.State.CONFIRMED)
    elif dep.state == Deployment.State.RESTARTING:
        _end(
            dep,
            Deployment.State.ROLLED_BACK,
            "rolled_back",
            f"came back running {scanner.fw or 'an unknown version'}",
        )
    else:
        _retry_or_fail(
            dep, "interrupted", "the scanner restarted before the update finished"
        )


def _notify(dep: Deployment) -> None:
    """Tell whoever asked for it that an update did not take."""
    if dep.requested_by is None:
        return
    try:
        from common.notifications import trigger_notification

        trigger_notification(
            dep,
            "nfcscanner.firmware_failed",
            targets=[dep.requested_by],
            context={
                "name": f"Scanner update {dep.get_state_display().lower()}",
                "message": f"{dep.scanner.reader_id}: firmware {dep.firmware.version} "
                f"{dep.get_state_display().lower()}. {dep.detail}",
            },
        )
    except Exception:  # noqa: BLE001 - a notification is a courtesy
        logger.debug("NFC: could not notify about deployment %s", dep.pk, exc_info=True)


# Deploying


def deploy(
    fw: Firmware,
    scanners,
    *,
    user=None,
    required=None,
    required_after=None,
    allow_downgrade=False,
) -> list[dict]:
    """Tell each scanner to run `fw`. Returns one {"reader", "deployment" | "refused"} per
    scanner. A pending deployment for the same scanner is superseded."""
    why = fwlib.compatible(fw)
    if why:
        raise Refused(f"{fw.version} cannot be deployed: {why}")
    results = []
    for scanner in scanners:
        try:
            dep = _deploy_one(
                fw,
                scanner,
                user=user,
                required=required,
                required_after=required_after,
                allow_downgrade=allow_downgrade,
            )
            results.append({"reader": scanner.reader_id, "deployment": dep.pk})
        except Refused as exc:
            results.append({"reader": scanner.reader_id, "refused": str(exc)})
    return results


def _deploy_one(fw, scanner, *, user, required, required_after, allow_downgrade):
    with transaction.atomic():
        scanner = Scanner.objects.select_for_update().get(pk=scanner.pk)
        if Deployment.objects.filter(
            scanner=scanner, state__in=Deployment.IN_FLIGHT
        ).exists():
            raise Refused("an update is in progress on it; wait for it to finish")
        if scanner.fw == fw.version:
            raise Refused(f"it runs {fw.version} already")
        if scanner.fw and fwlib.newer(scanner.fw, fw.version):
            current = Firmware.objects.filter(version=scanner.fw).first()
            if (
                current
                and fw.settings_version < current.settings_version
                and network_machine(scanner.reader_id) is not None
            ):
                raise Refused(
                    f"{fw.version} cannot read the network settings {scanner.fw} keeps; "
                    "the scanner would drop off the network"
                )
            if not allow_downgrade:
                raise Refused(
                    f"{fw.version} is older than the {scanner.fw} it runs; confirm the downgrade"
                )
        now = timezone.now()
        for old in Deployment.objects.filter(
            scanner=scanner, state=Deployment.State.PENDING
        ):
            old.state = Deployment.State.SUPERSEDED
            old.finished_at = now
            old.detail = f"replaced by a deployment of {fw.version}"
            old.save()
        return Deployment.objects.create(
            scanner=scanner,
            firmware=fw,
            requested_by=user,
            required=required,
            required_after=required_after,
        )


def cancel(dep: Deployment) -> Deployment:
    """Withdraw a deployment the scanner has not started on."""
    with transaction.atomic():
        dep = Deployment.objects.select_for_update().get(pk=dep.pk)
        if dep.state != Deployment.State.PENDING:
            raise Refused(
                "only a pending deployment can be cancelled; this one is "
                + dep.get_state_display().lower()
            )
        dep.state = Deployment.State.CANCELLED
        dep.finished_at = timezone.now()
        dep.save()
    return dep


def auto_deploy(added: list[str]) -> dict | None:
    """With the setting on, a new stable release that is the newest this plugin can drive
    goes to every scanner running something older. A courtesy: it never raises. Returns what
    it did, for the check's record, or None when it had nothing to do.

    A newer release this plugin cannot drive (another protocol, or one that needs a newer
    plugin) is held and shown, but not deployed; the newest one it can drive is, if it is
    among those just fetched."""
    if not setting("FIRMWARE_AUTO_DEPLOY", False):
        return None
    stable = [v for v in added if "-" not in v]
    candidate = newest_firmware(include_prereleases=False)
    if candidate is None or candidate.version not in stable:
        return None
    scanners = [
        s
        for s in Scanner.objects.all()
        if not s.fw or fwlib.newer(candidate.version, s.fw)
    ]
    try:
        results = deploy(candidate, scanners)
    except Refused as exc:
        logger.warning(
            "NFC scanner firmware %s not deployed automatically: %s",
            candidate.version,
            exc,
        )
        return {"version": candidate.version, "refused": str(exc)}
    logger.info(
        "NFC scanner firmware %s deployed automatically: %s", candidate.version, results
    )
    return {"version": candidate.version, "results": results}


def newest_firmware(
    include_prereleases: bool = True, compatible_only: bool = True
) -> Firmware | None:
    """The newest release held, by version: by default only among those this plugin can
    deploy (its protocol, its plugin version, its image still on disk)."""
    qs = Firmware.objects.exclude(app="")
    if not include_prereleases:
        qs = qs.filter(prerelease=False)
    if compatible_only:
        qs = [fw for fw in qs if not fwlib.compatible(fw)]
    return fwlib.newest(qs)


# Network


def image_path(fw: Firmware) -> str:
    return f"/plugin/nfcscanner/firmware/{fw.version}/{fw.manifest['app']['file']}"


def network_downloads_in_flight() -> int:
    return Deployment.objects.filter(
        via=Scanner.Via.NETWORK,
        state__in=[Deployment.State.SENT, Deployment.State.DOWNLOADING],
    ).count()


def issue_network(machine, scanner: Scanner, origin: str) -> None:
    """Called on each /sync: start a pending update if the scanner is free for it. `origin`
    is the scheme and host the scanner called, which is where it will fetch the image from."""
    from .sync import enqueue

    now = timezone.now()
    dep = (
        Deployment.objects.filter(scanner=scanner, state=Deployment.State.PENDING)
        .select_related("firmware")
        .first()
    )
    if dep is None or (dep.retry_after and dep.retry_after > now):
        return
    if fwlib.compatible(dep.firmware):
        _end(
            dep, Deployment.State.FAILED, "incompatible", fwlib.compatible(dep.firmware)
        )
        return
    if (
        Job.objects.filter(machine=machine.machine_config)
        .exclude(state__in=Job.FINISHED)
        .exists()
    ):
        return  # a job first; the update on a later call
    if network_downloads_in_flight() >= int(setting("FIRMWARE_MAX_DOWNLOADS", 2)):
        return
    with transaction.atomic():
        dep = (
            Deployment.objects.select_for_update()
            .filter(pk=dep.pk, state=Deployment.State.PENDING)
            .first()
        )
        if dep is None:
            return
        dep.state = Deployment.State.SENT
        dep.via = Scanner.Via.NETWORK
        dep.from_version = scanner.fw
        dep.boot_at_start = scanner.boot
        dep.started_at = now
        dep.attempts += 1
        dep.retry_after = None
        dep.error = ""
        dep.detail = ""
        dep.save()
        enqueue(
            machine.machine_config,
            {
                "cmd": "ota",
                "id": dep.pk,
                "url": origin.rstrip("/") + image_path(dep.firmware),
                "sha256": dep.firmware.app_sha256,
            },
        )
    logger.info(
        "NFC scanner %s: sent firmware %s (attempt %d)",
        scanner.reader_id,
        dep.firmware.version,
        dep.attempts,
    )


def _retire_ota(dep: Deployment) -> None:
    """The deployment's command, if the scanner has not taken it yet, is no longer wanted."""
    from .models import ScannerCommand
    from .sync import retire

    retire(
        ScannerCommand.objects.filter(
            acked_at__isnull=True, payload__cmd="ota", payload__id=dep.pk
        )
    )


def apply_ota_message(scanner: Scanner, msg: dict) -> None:
    """The scanner's answer to `ota`, or an `ota` event, moves its network deployment."""
    with transaction.atomic():
        dep = (
            Deployment.objects.select_for_update()
            .filter(scanner=scanner, state__in=Deployment.IN_FLIGHT)
            .first()
        )
        if dep is None:
            return
        error = msg.get("error") if isinstance(msg.get("error"), str) else ""
        detail = msg.get("detail") if isinstance(msg.get("detail"), str) else ""
        if msg.get("rsp") == "ota":
            if msg.get("id") != dep.pk or msg.get("ok") is True:
                return
            if error == "busy":
                dep.attempts = max(0, dep.attempts - 1)  # not the update's fault
                _retry_or_fail(dep, "busy", detail, after=timezone.now() + BUSY_RETRY)
            else:
                _end(dep, Deployment.State.FAILED, error or "refused", detail)
            return
        state = msg.get("state")
        if state == "downloading" and dep.state == Deployment.State.SENT:
            dep.state = Deployment.State.DOWNLOADING
            dep.save()
        elif state == "restarting" and dep.state in (
            Deployment.State.SENT,
            Deployment.State.DOWNLOADING,
        ):
            dep.state = Deployment.State.RESTARTING
            dep.save()
        elif state == "failed":
            _end(dep, Deployment.State.FAILED, error or "failed", detail)


# USB


def is_required(dep: Deployment) -> bool:
    if dep.required_after and dep.required_after <= timezone.now():
        return True
    if dep.required is not None:
        return dep.required
    return setting("FIRMWARE_USB_POLICY", POLICY_DEFERRABLE) == POLICY_REQUIRED


def usb_offer(scanner: Scanner) -> Deployment | None:
    """The update waiting for a scanner a browser has just connected to, if any."""
    dep = (
        Deployment.objects.filter(scanner=scanner, state=Deployment.State.PENDING)
        .select_related("firmware")
        .first()
    )
    if dep is None or fwlib.compatible(dep.firmware):
        return None
    return dep


def usb_claim(dep: Deployment, user) -> Deployment:
    with transaction.atomic():
        dep = (
            Deployment.objects.select_for_update()
            .select_related("scanner", "firmware")
            .get(pk=dep.pk)
        )
        if dep.state != Deployment.State.PENDING:
            raise Refused("this update is not waiting any more")
        dep.state = Deployment.State.SENT
        dep.via = Scanner.Via.USB
        dep.from_version = dep.scanner.fw
        dep.boot_at_start = None
        dep.started_at = timezone.now()
        dep.attempts += 1
        dep.error = ""
        dep.detail = f"by {user.username}" if user else ""
        dep.save()
    return dep


def usb_defer(dep: Deployment) -> Deployment:
    with transaction.atomic():
        dep = Deployment.objects.select_for_update().get(pk=dep.pk)
        if dep.state != Deployment.State.PENDING:
            raise Refused("this update is not waiting any more")
        if is_required(dep):
            raise Refused("this update may not be put off")
        dep.deferrals += 1
        dep.last_deferred_at = timezone.now()
        dep.save()
    return dep


def usb_report(
    dep: Deployment, state: str, error: str = "", detail: str = ""
) -> Deployment:
    with transaction.atomic():
        dep = Deployment.objects.select_for_update().get(pk=dep.pk)
        if dep.via != Scanner.Via.USB or dep.state not in Deployment.IN_FLIGHT:
            raise Refused("this update is not in progress over USB")
        if state == "downloading" and dep.state == Deployment.State.SENT:
            dep.state = Deployment.State.DOWNLOADING
            dep.save()
        elif state == "restarting":
            dep.state = Deployment.State.RESTARTING
            dep.save()
        elif state == "failed":
            if error in ("busy", "interrupted", "cancelled"):
                _retry_or_fail(dep, error, detail)
            else:
                _end(dep, Deployment.State.FAILED, error or "failed", detail)
    return dep


# Housekeeping


def check_deployments() -> None:
    """Updates that have gone quiet. Run every minute."""
    now = timezone.now()
    for dep in Deployment.objects.filter(state__in=Deployment.IN_FLIGHT).select_related(
        "scanner", "firmware"
    ):
        with transaction.atomic():
            dep = (
                Deployment.objects.select_for_update()
                .filter(pk=dep.pk, state__in=Deployment.IN_FLIGHT)
                .select_related("scanner", "firmware")
                .first()
            )
            if dep is None:
                continue
            quiet = now - dep.updated_at
            if dep.via == Scanner.Via.USB:
                if dep.state == Deployment.State.RESTARTING and quiet > USB_TIMEOUT:
                    _end(
                        dep,
                        Deployment.State.FAILED,
                        "did_not_return",
                        "no browser has connected to it since it restarted",
                    )
                elif quiet > USB_TIMEOUT:
                    _retry_or_fail(
                        dep, "interrupted", "the browser went away mid-update"
                    )
            elif dep.state == Deployment.State.SENT and quiet > SENT_TIMEOUT:
                _retry_or_fail(
                    dep, "no_answer", "the scanner did not answer the update"
                )
            elif dep.state == Deployment.State.DOWNLOADING and quiet > DOWNLOAD_TIMEOUT:
                _end(
                    dep,
                    Deployment.State.FAILED,
                    "timeout",
                    "the download did not finish",
                )
            elif dep.state == Deployment.State.RESTARTING and quiet > RESTART_TIMEOUT:
                _end(
                    dep,
                    Deployment.State.FAILED,
                    "did_not_return",
                    "the scanner has not called since it restarted",
                )
