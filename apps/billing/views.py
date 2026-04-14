"""
Billing views.

GET  /billing/plans          — list all active plans (PUBLIC)
GET  /billing/subscription   — current org subscription (BILLING+)
POST /billing/subscribe      — change plan (OWNER only)
GET  /billing/invoices       — invoices for current org (BILLING+)
POST /billing/webhooks       — mock Stripe webhook handler (HMAC verified)
"""

import json
import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.billing.models import Invoice, Plan, Subscription
from apps.billing.serializers import (
    InvoiceSerializer,
    PlanSerializer,
    SubscribeSerializer,
    SubscriptionSerializer,
    WebhookSerializer,
)
from apps.billing.services import get_billing_service, verify_webhook_signature
from apps.rbac.permissions import CanManageBilling, CanReadBilling, IsOwner

logger = logging.getLogger(__name__)


# ── GET /billing/plans ─────────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([AllowAny])
def list_plans(request):
    """
    Return all active billing plans.

    Public — no authentication required so unauthenticated users can see
    available plans before signing up.
    """
    plans = Plan.objects.filter(is_active=True).order_by("price_monthly")
    serializer = PlanSerializer(plans, many=True)
    return Response(serializer.data)


# ── GET /billing/subscription ──────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([IsAuthenticated, CanReadBilling])
def get_subscription(request):
    """
    Return the current organization's subscription with plan details.

    Requires ``billing:read`` permission (BILLING+, VIEWER+, ADMIN, OWNER).
    """
    org = getattr(request, "org", None)
    if org is None:
        return Response(
            {"error": "No active organisation context.", "code": "no_org"},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        subscription = (
            Subscription.objects
            .select_related("plan")
            .get(organization=org)
        )
    except Subscription.DoesNotExist:
        return Response(
            {"error": "No active subscription found.", "code": "no_subscription"},
            status=status.HTTP_404_NOT_FOUND,
        )

    serializer = SubscriptionSerializer(subscription)
    return Response(serializer.data)


# ── POST /billing/subscribe ────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanManageBilling])
def subscribe(request):
    """
    Upgrade or downgrade the organization's plan.

    Requires ``billing:manage`` permission (OWNER and BILLING role only).
    Replaces any existing subscription atomically.
    """
    org = getattr(request, "org", None)
    if org is None:
        return Response(
            {"error": "No active organisation context.", "code": "no_org"},
            status=status.HTTP_403_FORBIDDEN,
        )

    serializer = SubscribeSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    plan = serializer.get_plan()

    billing = get_billing_service()
    subscription = billing.subscribe(org, plan)

    return Response(
        {
            "message": f"Successfully subscribed to the {plan.name} plan.",
            "subscription": SubscriptionSerializer(subscription).data,
        },
        status=status.HTTP_200_OK,
    )


# ── GET /billing/invoices ──────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([IsAuthenticated, CanReadBilling])
def list_invoices(request):
    """
    Return paginated invoice history for the current organization.

    Requires ``billing:read`` permission.
    """
    org = getattr(request, "org", None)
    if org is None:
        return Response(
            {"error": "No active organisation context.", "code": "no_org"},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        subscription = Subscription.objects.get(organization=org)
    except Subscription.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    invoices = (
        Invoice.objects
        .filter(subscription=subscription)
        .select_related("subscription__plan")
        .order_by("-created_at")
    )
    serializer = InvoiceSerializer(invoices, many=True)
    return Response(serializer.data)


# ── POST /billing/webhooks ─────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
def webhook_handler(request):
    """
    Receive and process mock Stripe webhook events.

    Security: verifies HMAC-SHA256 signature in ``X-Webhook-Signature``
    header before processing any payload. Requests without a valid
    signature are rejected with HTTP 400.

    This endpoint is intentionally unauthenticated (AllowAny) because
    real webhook handlers from Stripe/payment processors never carry
    user session tokens — they use signature-based authentication instead.
    """
    # ── Signature Verification ─────────────────────────────────────────────
    received_sig = request.headers.get("X-Webhook-Signature", "")
    if not received_sig:
        return Response(
            {"error": "Missing X-Webhook-Signature header.", "code": "missing_signature"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    payload_bytes = request.body
    if not verify_webhook_signature(payload_bytes, received_sig):
        logger.warning("webhook_handler: invalid signature received")
        return Response(
            {"error": "Invalid webhook signature.", "code": "invalid_signature"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Payload Validation ─────────────────────────────────────────────────
    serializer = WebhookSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    event_type = serializer.validated_data["event_type"]
    payload = serializer.validated_data["payload"]

    # ── Event Dispatch ─────────────────────────────────────────────────────
    try:
        result = get_billing_service().handle_webhook(event_type, payload)
    except ValueError as exc:
        return Response(
            {"error": str(exc), "code": "unsupported_event"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as exc:
        logger.error("webhook_handler: unexpected error for event %s: %s", event_type, exc)
        return Response(
            {"error": "Internal error processing webhook.", "code": "internal_error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response(result, status=status.HTTP_200_OK)
