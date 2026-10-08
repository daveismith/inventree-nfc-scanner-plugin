"""A per-scanner command counter that only ever goes up."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Add ScannerCounter."""

    dependencies = [
        ("inventree_nfc_scanner", "0001_initial"),
        ("machine", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScannerCounter",
            fields=[
                (
                    "machine",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="nfc_counter",
                        serialize=False,
                        to="machine.machineconfig",
                    ),
                ),
                ("last_seq", models.PositiveIntegerField(default=0)),
            ],
            options={"app_label": "inventree_nfc_scanner"},
        ),
    ]
