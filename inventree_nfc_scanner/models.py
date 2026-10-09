"""Jobs, and the bookkeeping behind the exchange with network scanners."""

from django.contrib.auth.models import User
from django.db import models
from django.utils.translation import gettext_lazy as _


class Job(models.Model):
    """One request to program (or wipe) a location's tag.

    A job with a machine goes to that network scanner through /sync. A job without one was
    done by the browser over USB, and is recorded here only so the history is complete.
    """

    # Declared, as the migrations have it, rather than left to DEFAULT_AUTO_FIELD, which
    # InvenTree sets to AutoField (so makemigrations would want to change it).
    id = models.BigAutoField(
        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
    )

    class Meta:
        """Meta options."""

        app_label = "inventree_nfc_scanner"
        ordering = ["-created_at"]
        verbose_name = _("NFC tag job")
        verbose_name_plural = _("NFC tag jobs")

    class Kind(models.TextChoices):
        """What the job does to the tag."""

        PROGRAM = "program", _("Program")
        WIPE = "wipe", _("Wipe")

    class State(models.TextChoices):
        """Where the job is. Mirrors the scanner's events."""

        QUEUED = "queued", _("Queued")  # not yet collected by the scanner
        SENT = "sent", _("Sent")  # collected, not yet answered
        WAITING = "waiting", _("Waiting for a tag")
        WRITING = "writing", _("Writing")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    FINISHED = (State.DONE, State.FAILED, State.CANCELLED)

    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.PROGRAM)
    location = models.ForeignKey(
        "stock.StockLocation",
        on_delete=models.CASCADE,
        related_name="nfc_jobs",
        verbose_name=_("Location"),
    )
    machine = models.ForeignKey(
        "machine.MachineConfig",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="nfc_jobs",
        verbose_name=_("Scanner"),
        help_text=_("The network scanner; empty for a job done over USB"),
    )
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    state = models.CharField(max_length=10, choices=State.choices, default=State.QUEUED)
    overwrite = models.BooleanField(default=False)
    timeout_s = models.PositiveIntegerField(default=60)

    uid = models.CharField(
        max_length=20, blank=True, help_text=_("The tag written, as reported")
    )
    tag_type = models.CharField(max_length=12, blank=True)
    protected = models.BooleanField(null=True, blank=True)
    error = models.CharField(max_length=32, blank=True)
    error_detail = models.CharField(max_length=200, blank=True)

    # What the tag already held, when a job was refused with not_blank
    existing_text = models.CharField(max_length=128, blank=True)
    existing_uri = models.CharField(max_length=256, blank=True)

    def __str__(self) -> str:
        """Readable form."""
        return f"{self.get_kind_display()} tag for {self.location} ({self.get_state_display()})"

    @property
    def finished(self) -> bool:
        """Whether nothing more will happen to this job."""
        return self.state in self.FINISHED


class ScannerCommand(models.Model):
    """A command queued for a network scanner, kept until the scanner acknowledges it."""

    # As the migrations have it (see Job.id).
    id = models.BigAutoField(
        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
    )

    class Meta:
        """Meta options."""

        app_label = "inventree_nfc_scanner"
        ordering = ["seq"]
        unique_together = [("machine", "seq")]

    machine = models.ForeignKey(
        "machine.MachineConfig", on_delete=models.CASCADE, related_name="nfc_commands"
    )
    seq = models.PositiveIntegerField()
    job = models.ForeignKey(
        Job, null=True, blank=True, on_delete=models.CASCADE, related_name="commands"
    )
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    acked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        """Readable form."""
        return f"{self.machine_id} #{self.seq} {self.payload.get('cmd')}"


class ScannerMessage(models.Model):
    """A message a network scanner has sent, by number, so a repeat is recognised.

    Only the identity is kept; the content was applied when it first arrived.
    """

    # As the migrations have it (see Job.id).
    id = models.BigAutoField(
        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
    )

    class Meta:
        """Meta options."""

        app_label = "inventree_nfc_scanner"
        unique_together = [("machine", "boot", "seq")]

    machine = models.ForeignKey(
        "machine.MachineConfig", on_delete=models.CASCADE, related_name="nfc_messages"
    )
    boot = models.PositiveIntegerField()
    seq = models.PositiveIntegerField()
    received_at = models.DateTimeField(auto_now_add=True)


class ScannerCounter(models.Model):
    """The last command number given to a scanner. It only goes up, whatever becomes of the
    command rows themselves (pruned, or deleted along with a job or a location), so a number
    the scanner has already acknowledged is never given out again."""

    class Meta:
        """Meta options."""

        app_label = "inventree_nfc_scanner"

    machine = models.OneToOneField(
        "machine.MachineConfig",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="nfc_counter",
    )
    last_seq = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        """Readable form."""
        return f"{self.machine_id} at {self.last_seq}"


# Fleet firmware updates. See docs/fleet-updates.md.


def firmware_path(instance, filename: str) -> str:
    """Where an image is kept in media: one folder per version."""
    return f"plugins/nfcscanner/firmware/{instance.version}/{filename}"


class Firmware(models.Model):
    """A scanner firmware release the server holds, fetched from GitHub or uploaded.

    The manifest is the release's own description (tools/make_release.py in the firmware
    repository); the fields beside it are the parts of it the plugin decides with.
    """

    # As the migrations have it (see Job.id).
    id = models.BigAutoField(
        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
    )

    class Meta:
        """Meta options."""

        app_label = "inventree_nfc_scanner"
        ordering = ["-published_at", "-pk"]
        verbose_name = _("Scanner firmware")
        verbose_name_plural = _("Scanner firmware")

    class Source(models.TextChoices):
        """Where it came from."""

        GITHUB = "github", _("GitHub release")
        UPLOAD = "upload", _("Uploaded")

    version = models.CharField(max_length=40, unique=True)
    prerelease = models.BooleanField(default=False)
    source = models.CharField(max_length=8, choices=Source.choices)
    release_url = models.URLField(max_length=300, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)
    added_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    manifest = models.JSONField()
    proto = models.PositiveIntegerField()
    settings_version = models.PositiveIntegerField()
    min_plugin = models.CharField(max_length=40)

    app = models.FileField(upload_to=firmware_path, max_length=200, blank=True)
    app_size = models.PositiveIntegerField()
    app_sha256 = models.CharField(max_length=64)
    merged = models.FileField(upload_to=firmware_path, max_length=200, blank=True)
    merged_size = models.PositiveIntegerField(null=True, blank=True)
    merged_sha256 = models.CharField(max_length=64, blank=True)

    def __str__(self) -> str:
        """Readable form."""
        return self.version

    @property
    def available(self) -> bool:
        """Whether the image is still on disk (old ones are pruned, their records kept)."""
        return bool(self.app)


class Scanner(models.Model):
    """Every scanner the server has heard of, over the network or through a browser over USB,
    by the id it reports (`nfc-` and its MAC address)."""

    # As the migrations have it (see Job.id).
    id = models.BigAutoField(
        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
    )

    class Meta:
        """Meta options."""

        app_label = "inventree_nfc_scanner"
        ordering = ["reader_id"]
        verbose_name = _("NFC scanner (fleet)")
        verbose_name_plural = _("NFC scanners (fleet)")

    class Via(models.TextChoices):
        """How it was last heard from."""

        NETWORK = "network", _("Network")
        USB = "usb", _("USB")

    reader_id = models.CharField(max_length=32, unique=True)
    fw = models.CharField(max_length=40, blank=True)
    proto = models.PositiveIntegerField(null=True, blank=True)
    boot = models.PositiveIntegerField(null=True, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_via = models.CharField(max_length=8, choices=Via.choices, blank=True)
    last_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    def __str__(self) -> str:
        """Readable form."""
        return f"{self.reader_id} ({self.fw or 'version unknown'})"


class Deployment(models.Model):
    """One scanner told to run one firmware. A network scanner gets it on its next call; a
    USB scanner when a browser next connects to it."""

    # As the migrations have it (see Job.id).
    id = models.BigAutoField(
        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
    )

    class Meta:
        """Meta options."""

        app_label = "inventree_nfc_scanner"
        ordering = ["-requested_at", "-pk"]

    class State(models.TextChoices):
        """Where it is."""

        PENDING = "pending", _("Pending")  # waiting for the scanner
        SENT = "sent", _("Sent")  # handed to the scanner, not yet answered
        DOWNLOADING = "downloading", _("Downloading")
        RESTARTING = "restarting", _("Restarting")
        CONFIRMED = "confirmed", _("Confirmed")
        FAILED = "failed", _("Failed")
        ROLLED_BACK = "rolled_back", _("Rolled back")
        SUPERSEDED = "superseded", _("Superseded")
        CANCELLED = "cancelled", _("Cancelled")

    IN_FLIGHT = (State.SENT, State.DOWNLOADING, State.RESTARTING)
    FINISHED = (
        State.CONFIRMED,
        State.FAILED,
        State.ROLLED_BACK,
        State.SUPERSEDED,
        State.CANCELLED,
    )

    scanner = models.ForeignKey(
        Scanner, on_delete=models.CASCADE, related_name="deployments"
    )
    firmware = models.ForeignKey(
        Firmware, on_delete=models.PROTECT, related_name="deployments"
    )
    requested_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    required = models.BooleanField(
        null=True,
        blank=True,
        help_text=_("Whether a USB user may put it off; empty: the plugin's policy"),
    )
    required_after = models.DateTimeField(
        null=True, blank=True, help_text=_("No longer deferrable from this time")
    )

    state = models.CharField(
        max_length=12, choices=State.choices, default=State.PENDING
    )
    via = models.CharField(max_length=8, choices=Scanner.Via.choices, blank=True)
    from_version = models.CharField(max_length=40, blank=True)
    boot_at_start = models.PositiveIntegerField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    retry_after = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    deferrals = models.PositiveIntegerField(default=0)
    last_deferred_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=32, blank=True)
    detail = models.CharField(max_length=200, blank=True)

    def __str__(self) -> str:
        """Readable form."""
        return (
            f"{self.firmware} to {self.scanner.reader_id} ({self.get_state_display()})"
        )

    @property
    def finished(self) -> bool:
        """Whether nothing more will happen to it."""
        return self.state in self.FINISHED
