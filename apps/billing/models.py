"""
Billing models.

Plan           — a product tier (FREE / PRO / ENTERPRISE) with limit definitions.
Subscription   — links an Organization to its active Plan; tracks lifecycle state.
Invoice        — individual billing record per billing period.
UsageRecord    — hourly/daily aggregated API usage per org, flushed from Redis.
"""

import uuid

from django.db import models
from django.utils import timezone

from apps.core.mixins import TimeStampedModel


# ── Plan ───────────────────────────────────────────────────────────────────────


class Plan(TimeStampedModel):
    """
    A billing plan tier.

    `limits` JSON shape::

        {
            "members_count": 5,
            "api_calls_per_month": 10000,
            "storage_mb": 1024
        }

    `features` JSON shape::

        {
            "audit_logs": false,
            "feature_flags": false,
            "sso": false
        }
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=50, unique=True, db_index=True)
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    limits = models.JSONField(
        default=dict,
        help_text="Hard limits: members_count, api_calls_per_month, storage_mb",
    )
    features = models.JSONField(
        default=dict,
        help_text="Feature flags enabled on this plan: audit_logs, feature_flags, sso",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["price_monthly"]
        verbose_name = "Plan"
        verbose_name_plural = "Plans"

    def __str__(self):
        return f"{self.name} (${self.price_monthly}/mo)"


# ── Subscription ───────────────────────────────────────────────────────────────


class SubscriptionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAST_DUE = "past_due", "Past Due"
    CANCELED = "canceled", "Canceled"


class Subscription(TimeStampedModel):
    """
    An organization's active plan subscription.

    One org has at most one active subscription at a time. When a plan change
    is made, the old subscription is canceled and a new one is created.

    `grace_period_end` is set 7 days after a limit is first exceeded. Hard
    enforcement (blocking requests) does not begin until this date is in the
    past. Set to None when the org comes back under limit.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name="subscriptions",
    )
    status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE,
        db_index=True,
    )
    current_period_start = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at = models.DateTimeField(null=True, blank=True)
    # Grace period: starts when a limit is first exceeded; hard block begins after.
    grace_period_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Subscription"
        verbose_name_plural = "Subscriptions"

    def __str__(self):
        return f"{self.organization} → {self.plan.name} ({self.status})"

    @property
    def is_in_grace_period(self) -> bool:
        """True if currently within the 7-day grace window."""
        if self.grace_period_end is None:
            return False
        return timezone.now() < self.grace_period_end

    @property
    def grace_period_expired(self) -> bool:
        """True if grace period was set and has elapsed — hard enforcement kicks in."""
        if self.grace_period_end is None:
            return False
        return timezone.now() >= self.grace_period_end


# ── Invoice ────────────────────────────────────────────────────────────────────


class InvoiceStatus(models.TextChoices):
    OPEN = "open", "Open"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    VOID = "void", "Void"


class Invoice(TimeStampedModel):
    """
    A billing invoice for one period of subscription.

    In mock mode the `stripe_invoice_id` holds a generated reference such as
    ``mock_inv_<uuid>``. The real Stripe integration can replace this with the
    actual ``in_`` prefixed Stripe invoice ID.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="invoices",
    )
    amount_cents = models.PositiveIntegerField(help_text="Amount in cents (USD)")
    status = models.CharField(
        max_length=20,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.OPEN,
        db_index=True,
    )
    stripe_invoice_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="Stripe invoice ID or mock reference",
    )
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Invoice"
        verbose_name_plural = "Invoices"

    def __str__(self):
        amount = f"${self.amount_cents / 100:.2f}"
        return f"Invoice {self.stripe_invoice_id} — {amount} ({self.status})"


# ── UsageRecord ────────────────────────────────────────────────────────────────


class UsageRecord(TimeStampedModel):
    """
    Aggregated API usage flushed from Redis into PostgreSQL.

    Written by the hourly Celery Beat task that reads Redis INCR counters
    and persists them here. The matching Redis keys are deleted after a
    successful flush to avoid double-counting.

    Metric names follow the pattern: ``api_calls``, ``storage_mb``, etc.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="usage_records",
        db_index=True,
    )
    metric_name = models.CharField(max_length=100, db_index=True)
    quantity = models.PositiveBigIntegerField(default=0)
    period_start = models.DateTimeField(db_index=True)
    period_end = models.DateTimeField()

    class Meta:
        ordering = ["-period_start"]
        indexes = [
            models.Index(fields=["organization", "metric_name", "period_start"]),
            models.Index(fields=["period_start"]),
        ]
        verbose_name = "Usage Record"
        verbose_name_plural = "Usage Records"

    def __str__(self):
        return (
            f"{self.organization} — {self.metric_name}: {self.quantity} "
            f"({self.period_start:%Y-%m-%d})"
        )
