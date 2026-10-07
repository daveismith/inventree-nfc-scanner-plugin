"""Jobs, commands and messages."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Initial."""

    initial = True

    dependencies = [
        ("stock", "0001_initial"),
        ("machine", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Job",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("program", "Program"), ("wipe", "Wipe")],
                        default="program",
                        max_length=8,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("sent", "Sent"),
                            ("waiting", "Waiting for a tag"),
                            ("writing", "Writing"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="queued",
                        max_length=10,
                    ),
                ),
                ("overwrite", models.BooleanField(default=False)),
                ("timeout_s", models.PositiveIntegerField(default=60)),
                (
                    "uid",
                    models.CharField(
                        blank=True,
                        help_text="The tag written, as reported",
                        max_length=20,
                    ),
                ),
                ("tag_type", models.CharField(blank=True, max_length=12)),
                ("protected", models.BooleanField(blank=True, null=True)),
                ("error", models.CharField(blank=True, max_length=32)),
                ("error_detail", models.CharField(blank=True, max_length=200)),
                ("existing_text", models.CharField(blank=True, max_length=128)),
                ("existing_uri", models.CharField(blank=True, max_length=256)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nfc_jobs",
                        to="stock.stocklocation",
                        verbose_name="Location",
                    ),
                ),
                (
                    "machine",
                    models.ForeignKey(
                        blank=True,
                        help_text="The network scanner; empty for a job done over USB",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="nfc_jobs",
                        to="machine.machineconfig",
                        verbose_name="Scanner",
                    ),
                ),
            ],
            options={
                "verbose_name": "NFC tag job",
                "verbose_name_plural": "NFC tag jobs",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="ScannerCommand",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("seq", models.PositiveIntegerField()),
                ("payload", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("acked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="commands",
                        to="inventree_nfc_scanner.job",
                    ),
                ),
                (
                    "machine",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nfc_commands",
                        to="machine.machineconfig",
                    ),
                ),
            ],
            options={
                "ordering": ["seq"],
                "unique_together": {("machine", "seq")},
            },
        ),
        migrations.CreateModel(
            name="ScannerMessage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("boot", models.PositiveIntegerField()),
                ("seq", models.PositiveIntegerField()),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                (
                    "machine",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nfc_messages",
                        to="machine.machineconfig",
                    ),
                ),
            ],
            options={
                "unique_together": {("machine", "boot", "seq")},
            },
        ),
    ]
