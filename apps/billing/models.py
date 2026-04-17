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


# ── Idempotency ────────────────────────────────────────────────────────────────


class IdempotencyKey(TimeStampedModel):
    """
    Stores results of idempotent API operations for replay protection.
    
    When a client sends a request with an Idempotency-Key header:
    1. Check if key exists in cache/DB
    2. If yes, return cached result (avoiding duplicate work)
    3. If no, proceed with operation and store result
    
    Results are retained for 24 hours to prevent accidental replays.
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="idempotency_keys",
        db_index=True,
    )
    idempotency_key = models.CharField(
        max_length=255,
        help_text="Client-provided unique identifier (Idempotency-Key header)",
        db_index=True,
    )
    operation_type = models.CharField(
        max_length=100,
        help_text="Type of operation (e.g., 'subscribe', 'cancel')",
        db_index=True,
    )
    request_hash = models.CharField(
        max_length=64,
        help_text="SHA256 hash of request body for validation",
    )
    response_status = models.IntegerField(help_text="HTTP status code of result")
    response_data = models.JSONField(help_text="Cached response body")
    error_message = models.TextField(null=True, blank=True, help_text="Error if operation failed")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "idempotency_key"]),
            models.Index(fields=["created_at"]),
        ]
        verbose_name = "Idempotency Key"
        verbose_name_plural = "Idempotency Keys"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                name="unique_org_idempotency_key",
            )
        ]

    def __str__(self):
        return f"{self.organization} — {self.idempotency_key[:16]}... ({self.operation_type})"


# ── Webhook Events ─────────────────────────────────────────────────────────────


class WebhookEventStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"
    DEAD_LETTER = "dead_letter", "Dead Letter"


class WebhookEvent(TimeStampedModel):
    """
    Stores incoming webhook events for replay protection and audit.
    
    Provides:
    - Idempotent webhook delivery (replay protection via event_id)
    - Audit trail of all webhook events (processed or failed)
    - Dead-letter queue for malformed but signed events
    - Retry mechanism for failed events
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Stripe event ID or mock event ID (unique)",
    )
    event_type = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Event type (e.g., 'payment_succeeded')",
    )
    status = models.CharField(
        max_length=20,
        choices=WebhookEventStatus.choices,
        default=WebhookEventStatus.PENDING,
        db_index=True,
    )
    payload = models.JSONField(help_text="Complete webhook payload")
    signature = models.CharField(
        max_length=255,
        help_text="HMAC signature for verification",
    )
    
    # Processing metadata
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
        db_index=True,
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    
    # Dead-letter info
    dead_letter_reason = models.TextField(
        null=True,
        blank=True,
        help_text="Reason event was moved to dead-letter queue",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event_type", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["organization", "created_at"]),
        ]
        verbose_name = "Webhook Event"
        verbose_name_plural = "Webhook Events"

    def __str__(self):
        return f"{self.event_type} ({self.status}) — {self.event_id[:12]}..."


# ── Plan Limit Events ──────────────────────────────────────────────────────────


class PlanLimitEventType(models.TextChoices):
    WARNING = "limit_warning", "Limit Warning (80%)"
    CRITICAL = "limit_critical", "Limit Critical (100%)"
    GRACE_STARTED = "grace_started", "Grace Period Started"
    GRACE_EXPIRED = "grace_expired", "Grace Period Expired"
    LIMIT_RESOLVED = "limit_resolved", "Limit Resolved (Back Under)"


class PlanLimitEvent(TimeStampedModel):
    """
    Event stream for plan limit violations and grace periods.
    
    Provides:
    - Event log of limit violations for audit trail
    - UX notifications (email, in-app alerts)
    - Webhook forwarding to org's custom webhooks
    - Analytics on usage patterns
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="plan_limit_events",
        db_index=True,
    )
    event_type = models.CharField(
        max_length=20,
        choices=PlanLimitEventType.choices,
        db_index=True,
    )
    limit_type = models.CharField(
        max_length=100,
        help_text="Which limit was affected (members_count, api_calls_per_month, etc.)",
        db_index=True,
    )
    
    # Current usage metrics
    current_usage = models.PositiveBigIntegerField()
    limit_value = models.PositiveBigIntegerField()
    usage_percentage = models.PositiveSmallIntegerField(
        help_text="Usage as percentage (0-100+)"
    )
    
    # Context
    metadata = models.JSONField(
        default=dict,
        help_text="Additional context (plan name, period dates, etc.)",
    )
    
    # Notification state
    email_sent = models.BooleanField(default=False)
    webhook_sent = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "event_type"]),
            models.Index(fields=["event_type", "created_at"]),
            models.Index(fields=["organization", "created_at"]),
        ]
        verbose_name = "Plan Limit Event"
        verbose_name_plural = "Plan Limit Events"

    def __str__(self):
        pct = f"{self.usage_percentage}%"
        return f"{self.organization} — {self.event_type} ({self.limit_type}: {pct})"
