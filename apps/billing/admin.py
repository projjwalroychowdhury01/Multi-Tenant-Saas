"""Django admin registration for billing models."""

from django.contrib import admin

from apps.billing.models import (
    IdempotencyKey,
    Invoice,
    Plan,
    PlanLimitEvent,
    Subscription,
    UsageRecord,
    WebhookEvent,
)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "price_monthly", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "slug"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        "organization",
        "plan",
        "status",
        "current_period_start",
        "current_period_end",
        "grace_period_end",
    ]
    list_filter = ["status", "plan"]
    search_fields = ["organization__name", "organization__slug"]
    readonly_fields = ["id", "created_at", "updated_at"]
    raw_id_fields = ["organization", "plan"]


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = [
        "stripe_invoice_id",
        "get_org",
        "amount_cents",
        "status",
        "paid_at",
        "created_at",
    ]
    list_filter = ["status"]
    search_fields = ["stripe_invoice_id", "subscription__organization__name"]
    readonly_fields = ["id", "created_at", "updated_at"]

    @admin.display(description="Organization")
    def get_org(self, obj):
        return obj.subscription.organization.name


@admin.register(UsageRecord)
class UsageRecordAdmin(admin.ModelAdmin):
    list_display = ["organization", "metric_name", "quantity", "period_start", "period_end"]
    list_filter = ["metric_name"]
    search_fields = ["organization__name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "period_start"


# ── Idempotency ────────────────────────────────────────────────────────────


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ["organization", "idempotency_key", "operation_type", "response_status", "created_at"]
    list_filter = ["operation_type", "response_status"]
    search_fields = ["organization__name", "idempotency_key"]
    readonly_fields = ["id", "created_at", "updated_at"]

    def has_add_permission(self, request):
        # Prevent manual creation — only created by API
        return False


# ── Webhook Events ─────────────────────────────────────────────────────────


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ["event_type", "event_id", "status", "organization", "retry_count", "created_at"]
    list_filter = ["status", "event_type"]
    search_fields = ["event_id", "organization__name"]
    readonly_fields = ["id", "created_at", "updated_at", "payload", "signature"]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        # Prevent manual creation — only created by webhook handler
        return False

    fieldsets = (
        ("Event Metadata", {
            "fields": ("id", "event_id", "event_type", "organization"),
        }),
        ("Processing", {
            "fields": ("status", "processed_at", "retry_count", "error_message"),
        }),
        ("Dead Letter", {
            "fields": ("dead_letter_reason",),
            "classes": ("collapse",),
        }),
        ("Payload", {
            "fields": ("payload", "signature"),
            "classes": ("collapse",),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


# ── Plan Limit Events ──────────────────────────────────────────────────────


@admin.register(PlanLimitEvent)
class PlanLimitEventAdmin(admin.ModelAdmin):
    list_display = [
        "organization",
        "event_type",
        "limit_type",
        "usage_percentage",
        "email_sent",
        "webhook_sent",
        "created_at",
    ]
    list_filter = ["event_type", "limit_type", "email_sent", "webhook_sent"]
    search_fields = ["organization__name", "limit_type"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        # Prevent manual creation — only created by limit checking logic
        return False
