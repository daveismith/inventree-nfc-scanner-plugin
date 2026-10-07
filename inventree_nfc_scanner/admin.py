"""Django admin: the job history and the scanner bookkeeping, read mostly."""

from django.contrib import admin

from .models import Job, ScannerCommand, ScannerMessage


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    """Jobs."""

    list_display = (
        "id",
        "kind",
        "location",
        "machine",
        "state",
        "uid",
        "error",
        "created_by",
        "created_at",
        "finished_at",
    )
    list_filter = ("kind", "state", "machine")
    search_fields = ("uid", "location__name", "error")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ScannerCommand)
class ScannerCommandAdmin(admin.ModelAdmin):
    """Commands queued for scanners. The payload may carry the tag password until the command
    is acknowledged, so it is shown without it."""

    list_display = (
        "id",
        "machine",
        "seq",
        "job",
        "command",
        "created_at",
        "sent_at",
        "acked_at",
    )
    list_filter = ("machine",)
    exclude = ("payload",)
    readonly_fields = ("command",)

    @admin.display(description="payload")
    def command(self, obj):
        """The payload with its secrets replaced."""
        return {
            k: ("***" if k in ("pwd", "pack") else v) for k, v in obj.payload.items()
        }


@admin.register(ScannerMessage)
class ScannerMessageAdmin(admin.ModelAdmin):
    """Messages seen from scanners."""

    list_display = ("id", "machine", "boot", "seq", "received_at")
    list_filter = ("machine",)
