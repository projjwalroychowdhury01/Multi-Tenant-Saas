"""
Initial migration for the api_keys app.

Creates the ApiKey table with all required fields, indexes, and FK references.
"""

import django.db.models.deletion
import django.utils.timezone
import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenants", "0002_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ApiKey",
            fields=[
                # Core identity
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                # Timestamps from TenantModel / TimeStampedModel chain
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                # SoftDelete fields (inherited via TenantModel → SoftDeleteMixin)
                ("deleted_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                # Key fields
                ("name", models.CharField(max_length=255)),
                ("prefix", models.CharField(db_index=True, max_length=16, unique=True)),
                ("hashed_key", models.CharField(max_length=128)),
                (
                    "env",
                    models.CharField(
                        choices=[("live", "Live"), ("test", "Test")],
                        default="live",
                        max_length=10,
                    ),
                ),
                ("scopes", models.JSONField(blank=True, default=list)),
                ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                # Foreign keys
                (
                    "organization",
                    models.ForeignKey(
                        db_index=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="api_keys_apikey_set",
                        to="tenants.organization",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_api_keys",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "deleted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "API Key",
                "verbose_name_plural": "API Keys",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="apikey",
            index=models.Index(fields=["prefix", "is_active"], name="api_keys_ap_prefix_idx"),
        ),
        migrations.AddIndex(
            model_name="apikey",
            index=models.Index(fields=["organization", "is_active"], name="api_keys_ap_org_idx"),
        ),
    ]
