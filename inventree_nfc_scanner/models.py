"""Jobs, and the bookkeeping behind the exchange with network scanners."""

from django.contrib.auth.models import User
from django.db import models
from django.utils.translation import gettext_lazy as _


class Job(models.Model):
    """One request to program (or wipe) a location's tag.

    A job with a machine goes to that network scanner through /sync. A job without one was
    done by the browser over USB, and is recorded here only so the history is complete.
    """

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
