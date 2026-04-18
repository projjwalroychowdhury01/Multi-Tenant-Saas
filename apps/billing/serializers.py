"""
Billing serializers.

PlanSerializer         — read-only plan representation
SubscriptionSerializer — read-only subscription with plan detail nested
InvoiceSerializer      — read-only invoice representation
SubscribeSerializer    — validates plan change request
WebhookSerializer      — validates incoming mock Stripe webhook payloads
"""

from rest_framework import serializers

from apps.billing.models import Invoice, Plan, Subscription

# ── Plan ───────────────────────────────────────────────────────────────────────


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = [
            "id",
            "name",
            "slug",
            "price_monthly",
            "limits",
            "features",
        ]


# ── Subscription ───────────────────────────────────────────────────────────────


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    is_in_grace_period = serializers.BooleanField(read_only=True)
    grace_period_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            "id",
            "plan",
            "status",
            "current_period_start",
            "current_period_end",
            "cancel_at",
            "grace_period_end",
            "is_in_grace_period",
            "grace_period_expired",
            "created_at",
            "updated_at",
        ]


# ── Invoice ────────────────────────────────────────────────────────────────────


class InvoiceSerializer(serializers.ModelSerializer):
    amount_dollars = serializers.SerializerMethodField()
    plan_name = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "id",
            "stripe_invoice_id",
            "amount_cents",
            "amount_dollars",
            "status",
            "period_start",
            "period_end",
            "paid_at",
            "plan_name",
            "created_at",
        ]

    def get_amount_dollars(self, obj) -> str:
        return f"${obj.amount_cents / 100:.2f}"

    def get_plan_name(self, obj) -> str:
        return obj.subscription.plan.name


# ── Subscribe ─────────────────────────────────────────────────────────────────


class SubscribeSerializer(serializers.Serializer):
    """Validates a plan-change request."""

    plan_slug = serializers.SlugField()

    def validate_plan_slug(self, value):
        try:
            plan = Plan.objects.get(slug=value, is_active=True)
        except Plan.DoesNotExist:
            raise serializers.ValidationError(
                f"Plan '{value}' does not exist or is no longer available."
            )
        self._plan = plan
        return value

    def get_plan(self) -> Plan:
        """Return the resolved Plan instance after validation."""
        return self._plan


# ── Webhook ───────────────────────────────────────────────────────────────────


SUPPORTED_WEBHOOK_EVENTS = frozenset(
    [
        "payment_succeeded",
        "payment_failed",
        "subscription_canceled",
    ]
)


class WebhookSerializer(serializers.Serializer):
    """
    Validates the structure of incoming mock Stripe webhook payloads.

    Expected request body::

        {
            "event_type": "payment_succeeded",
            "payload": {
                "invoice_id": "mock_inv_abc123"
            }
        }
    """

    event_type = serializers.CharField()
    payload = serializers.DictField(child=serializers.JSONField(), allow_empty=True)

    def validate_event_type(self, value):
        if value not in SUPPORTED_WEBHOOK_EVENTS:
            raise serializers.ValidationError(
                f"Unsupported event type: '{value}'. "
                f"Supported events: {sorted(SUPPORTED_WEBHOOK_EVENTS)}"
            )
        return value
