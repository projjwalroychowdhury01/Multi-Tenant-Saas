"""
Migration: Add UUID-compatible polymorphic ID fields and snapshot metadata to ResourceSnapshot.

Changes:
- Convert resource_id, organization_id, actor_id to PolymorphicIDField
- Add snapshot_metadata JSONField
- Add additional database indexes for query optimization
- Update index names for consistency
"""

from django.db import migrations, models
import apps.features.models


class Migration(migrations.Migration):

    dependencies = [
        ("features", "0002_rename_features_fea_key_idx_features_fe_key_e00778_idx_and_more"),
    ]

    operations = [
        # Add snapshot_metadata field first
        migrations.AddField(
            model_name="resourcesnapshot",
            name="snapshot_metadata",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Additional metadata (e.g., request headers, tags, labels)",
            ),
        ),
        # Remove old indexes (they will be recreated with new field types)
        migrations.RemoveIndex(
            model_name="resourcesnapshot",
            name="features_res_resource_idx",
        ),
        migrations.RemoveIndex(
            model_name="resourcesnapshot",
            name="features_res_org_creat_idx",
        ),
        migrations.RemoveIndex(
            model_name="resourcesnapshot",
            name="features_res_resource_version_idx",
        ),
        # Convert ID fields to PolymorphicIDField
        migrations.AlterField(
            model_name="resourcesnapshot",
            name="resource_id",
            field=apps.features.models.PolymorphicIDField(
                help_text="Primary key of the resource (UUID or int)",
            ),
        ),
        migrations.AlterField(
            model_name="resourcesnapshot",
            name="organization_id",
            field=apps.features.models.PolymorphicIDField(
                blank=True,
                help_text="Tenant context for the resource (UUID or int)",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="resourcesnapshot",
            name="actor_id",
            field=apps.features.models.PolymorphicIDField(
                blank=True,
                help_text="User ID who triggered the change (UUID or int)",
                null=True,
            ),
        ),
        # Add database indexes for new field types
        migrations.AddIndex(
            model_name="resourcesnapshot",
            index=models.Index(
                fields=["resource_type", "resource_id"],
                name="features_res_type_id_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="resourcesnapshot",
            index=models.Index(
                fields=["organization_id", "created_at"],
                name="features_res_org_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="resourcesnapshot",
            index=models.Index(
                fields=["resource_type", "version"],
                name="features_res_type_version_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="resourcesnapshot",
            index=models.Index(
                fields=["resource_type", "organization_id"],
                name="features_res_type_org_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="resourcesnapshot",
            index=models.Index(
                fields=["actor_id", "created_at"],
                name="features_res_actor_created_idx",
            ),
        ),
        # Add db_index to resource_type
        migrations.AlterField(
            model_name="resourcesnapshot",
            name="resource_type",
            field=models.CharField(
                db_index=True,
                help_text="Model name (e.g., 'User', 'ApiKey', 'Organization')",
                max_length=100,
            ),
        ),
        # Add db_index to version
        migrations.AlterField(
            model_name="resourcesnapshot",
            name="version",
            field=models.IntegerField(
                db_index=True,
                help_text="Version number at the time of snapshot",
            ),
        ),
        # Add db_index to request_id
        migrations.AlterField(
            model_name="resourcesnapshot",
            name="request_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Request ID for correlation",
                max_length=100,
            ),
        ),
        # Add db_index to created_at
        migrations.AlterField(
            model_name="resourcesnapshot",
            name="created_at",
            field=models.DateTimeField(
                auto_now_add=True,
                db_index=True,
            ),
        ),
        # Update Meta options
        migrations.AlterModelOptions(
            name="resourcesnapshot",
            options={
                "ordering": ["-created_at"],
                "verbose_name_plural": "Resource Snapshots",
            },
        ),
    ]
