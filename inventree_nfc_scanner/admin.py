"""Django admin: the job history, the scanner bookkeeping and fleet updates, read mostly.

Every model is registered here. InvenTree reloads this module whenever one of the plugin's
models is missing from the admin site (AppMixin._reregister_contrib_apps), and a reload
re-registers the rest, which fails: a plugin registry reload then stops part way.
tests/server/test_versions.py checks none is left out.
"""

from django.contrib import admin

from .models import (
    Deployment,
    Firmware,
    Job,
    Scanner,
    ScannerCommand,
    ScannerCounter,
    ScannerMessage,
)


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


@admin.register(ScannerCounter)
class ScannerCounterAdmin(admin.ModelAdmin):
    """The command numbering per scanner; read only."""

    list_display = ("machine", "last_seq")
    readonly_fields = ("machine", "last_seq")


@admin.register(Firmware)
class FirmwareAdmin(admin.ModelAdmin):
    """Firmware releases held (managed from the fleet page)."""

    list_display = (
        "version",
        "prerelease",
        "source",
        "proto",
        "min_plugin",
        "added_at",
    )
    list_filter = ("source", "prerelease")
    readonly_fields = ("manifest", "app", "merged", "added_at")


@admin.register(Scanner)
class ScannerAdmin(admin.ModelAdmin):
    """Every scanner seen, over the network or USB."""

    list_display = ("reader_id", "fw", "last_via", "last_seen", "last_user")
    search_fields = ("reader_id",)


@admin.register(Deployment)
class DeploymentAdmin(admin.ModelAdmin):
    """Firmware updates, and their history."""

    list_display = (
        "id",
        "scanner",
        "firmware",
        "state",
        "via",
        "attempts",
        "requested_by",
        "requested_at",
        "finished_at",
    )
    list_filter = ("state", "via")
    readonly_fields = ("requested_at", "updated_at")
