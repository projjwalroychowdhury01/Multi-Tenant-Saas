"""
Initial migration for features app.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="FeatureFlag",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "key",
                    models.CharField(
                        help_text="Unique identifier for the feature flag (e.g. 'feature_x', 'beta_api_v2')",
                        max_length=100,
                        unique=True,
                    ),
                ),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        help_text="Human-readable description of what this flag controls",
                    ),
                ),
                (
                    "enabled_default",
                    models.BooleanField(
                        default=False, help_text="Default value if not explicitly set"
                    ),
                ),
                (
                    "enabled_for_plans",
                    models.JSONField(
                        default=dict,
                        help_text="Plan-specific enablement: {'free': bool, 'pro': bool, 'enterprise': bool}",
                    ),
                ),
                (
                    "enabled_for_orgs",
                    models.JSONField(
                        default=dict,
                        help_text="Organization-specific overrides: {org_id_str: true/false}",
                    ),
                ),
                (
                    "rollout_pct",
                    models.IntegerField(
                        default=0,
                        help_text="Percentage (0-100) of orgs for which this flag is enabled (via deterministic hash)",
                    ),
                ),
                (
                    "metadata",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Additional metadata (e.g. owner, ticket link, notes)",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True, help_text="Soft-disable the flag without deleting it"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="ResourceSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "resource_type",
                    models.CharField(
                        help_text="Model name (e.g. 'User', 'ApiKey', 'Organization')",
                        max_length=100,
                    ),
                ),
                ("resource_id", models.BigIntegerField(help_text="Primary key of the resource")),
                (
                    "organization_id",
                    models.BigIntegerField(
                        blank=True, help_text="Tenant context for the resource", null=True
                    ),
                ),
                (
                    "version",
                    models.IntegerField(help_text="Version number at the time of snapshot"),
                ),
                (
                    "data",
                    models.JSONField(
                        help_text="Complete JSON representation of the resource at this version"
                    ),
                ),
                (
                    "actor_id",
                    models.BigIntegerField(
                        blank=True, help_text="User ID who triggered the change", null=True
                    ),
                ),
                (
                    "request_id",
                    models.CharField(
                        blank=True, help_text="Request ID for correlation", max_length=100
                    ),
                ),
                (
                    "change_reason",
                    models.CharField(
                        blank=True,
                        help_text="Why the change was made (e.g. 'user_edit', 'admin_action', 'system_migration')",
                        max_length=255,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="resourcesnapshot",
            index=models.Index(
                fields=["resource_type", "resource_id"], name="features_res_resource_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="resourcesnapshot",
            index=models.Index(
                fields=["organization_id", "created_at"], name="features_res_org_creat_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="resourcesnapshot",
            index=models.Index(
                fields=["resource_type", "version"], name="features_res_resource_version_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="featureflag",
            index=models.Index(fields=["key"], name="features_fea_key_idx"),
        ),
        migrations.AddIndex(
            model_name="featureflag",
            index=models.Index(fields=["is_active"], name="features_fea_is_act_idx"),
        ),
    ]
