"""Django admin registration for ApiKey."""

from django.contrib import admin

from apps.api_keys.models import ApiKey


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "prefix",
        "env",
        "organization",
        "is_active",
        "expires_at",
        "last_used_at",
        "created_at",
    ]
    list_filter = ["env", "is_active", "organization"]
    search_fields = ["name", "prefix", "organization__name"]
    readonly_fields = [
        "id",
        "prefix",
        "hashed_key",
        "created_by",
        "last_used_at",
        "created_at",
        "updated_at",
    ]
    ordering = ["-created_at"]

    # Never show the hashed_key in any form — only in readonly fields for debugging
    fieldsets = [
        (None, {"fields": ["id", "name", "organization", "created_by"]}),
        ("Key Info", {"fields": ["prefix", "hashed_key", "env", "scopes"]}),
        ("Status", {"fields": ["is_active", "expires_at", "last_used_at"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"]}),
    ]
