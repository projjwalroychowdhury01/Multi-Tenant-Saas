"""Django admin registration for billing models."""

from django.contrib import admin

from apps.billing.models import Invoice, Plan, Subscription, UsageRecord


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
