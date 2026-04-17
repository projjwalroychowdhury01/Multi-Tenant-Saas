from django.contrib import admin

from .models import FeatureFlag, ResourceSnapshot


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ["key", "is_active", "rollout_pct", "enabled_default", "created_at"]
    list_filter = ["is_active", "created_at"]
    search_fields = ["key", "description"]
    fieldsets = (
        (
            "Basic",
            {
                "fields": (
                    "key",
                    "description",
                    "is_active",
                )
            },
        ),
        (
            "Defaults & Plan Config",
            {
                "fields": (
                    "enabled_default",
                    "enabled_for_plans",
                )
            },
        ),
        (
            "Per-Org Overrides & Rollout",
            {
                "fields": (
                    "enabled_for_orgs",
                    "rollout_pct",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": ("metadata",),
                "classes": ("collapse",),
            },
        ),
    )
    readonly_fields = ["created_at", "updated_at"]


@admin.register(ResourceSnapshot)
class ResourceSnapshotAdmin(admin.ModelAdmin):
    list_display = [
        "resource_type",
        "resource_id",
        "version",
        "organization_id",
        "created_at",
    ]
    list_filter = ["resource_type", "created_at"]
    search_fields = ["resource_type", "resource_id"]
    fieldsets = (
        (
            "Resource",
            {
                "fields": (
                    "resource_type",
                    "resource_id",
                    "version",
                )
            },
        ),
        (
            "Data",
            {
                "fields": ("data",),
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "organization_id",
                    "actor_id",
                    "request_id",
                    "change_reason",
                    "created_at",
                )
            },
        ),
    )
    readonly_fields = [
        "resource_type",
        "resource_id",
        "version",
        "data",
        "organization_id",
        "actor_id",
        "request_id",
        "change_reason",
        "created_at",
    ]
