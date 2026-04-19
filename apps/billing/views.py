"""
Billing views.

GET  /billing/plans          — list all active plans (PUBLIC)
GET  /billing/subscription   — current org subscription (BILLING+)
POST /billing/subscribe      — change plan (OWNER only)
GET  /billing/invoices       — invoices for current org (BILLING+)
POST /billing/webhooks       — mock Stripe webhook handler (HMAC verified)
"""

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
)
from apps.billing.services import get_billing_service, verify_webhook_signature
from apps.rbac.permissions import CanManageBilling, CanReadBilling

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
        subscription = Subscription.objects.select_related("plan").get(organization=org)
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
    Upgrade or downgrade the organization's plan with idempotency support.

    Requires ``billing:manage`` permission (OWNER and BILLING role only).
    Replaces any existing subscription atomically.

    Supports Idempotency-Key header for replay protection:
    - If provided, retried requests return the same result
    - Prevents duplicate charges if request is retried

    Example:
        POST /billing/subscribe
        Idempotency-Key: my-request-id-12345
        {"plan_id": "pro"}
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

    # Apply idempotency decorator manually for this function
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key:
        from apps.billing.idempotency import IdempotencyManager

        # Check for cached result
        cached = IdempotencyManager.get_result(str(org.id), idempotency_key)
        if cached:
            logger.info(
                f"Returning cached subscription result for idempotency key {idempotency_key}"
            )
            return Response(
                cached["data"],
                status=cached["status"],
            )

    # Perform subscription
    billing = get_billing_service()
    subscription = billing.subscribe(org, plan)

    response_data = {
        "message": f"Successfully subscribed to the {plan.name} plan.",
        "subscription": SubscriptionSerializer(subscription).data,
    }

    # Store result for idempotency
    if idempotency_key:
        from apps.billing.idempotency import IdempotencyManager

        IdempotencyManager.store_result(
            org_id=str(org.id),
            idempotency_key=idempotency_key,
            operation_type="subscribe",
            request_body=request.body or b"{}",
            response_status=status.HTTP_200_OK,
            response_data=response_data,
        )

    return Response(response_data, status=status.HTTP_200_OK)


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
        Invoice.objects.filter(subscription=subscription)
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
    Receive and process webhook events with strict validation.

    Security:
    - Verifies HMAC-SHA256 signature in ``X-Webhook-Signature`` header
    - Validates event payload schema (required/optional fields, type checking)
    - Implements replay protection via event_id deduplication
    - Routes malformed but signed events to dead-letter queue for manual review

    Idempotency:
    - Events are deduplicated by event_id (unique constraint in DB)
    - Retried webhooks return same result (cached in DB)

    Error Handling:
    - Invalid signature → 400 (malicious/corrupted)
    - Schema validation failure → 400 + dead-letter queue
    - Processing error → 500 + retry by webhook provider

    This endpoint is intentionally unauthenticated (AllowAny) because
    real webhook handlers never carry user session tokens — they use
    signature-based authentication instead.
    """
    from apps.billing.webhook_validation import (
        queue_dead_letter_event,
        validate_webhook_payload_signature_and_schema,
    )

    # ── Signature Verification ─────────────────────────────────────────────
    received_sig = request.headers.get("X-Webhook-Signature", "")
    if not received_sig:
        logger.warning("webhook_handler: missing X-Webhook-Signature header")
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

    # ── Payload Parsing ────────────────────────────────────────────────────
    try:
        import json

        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        logger.error(f"webhook_handler: invalid JSON: {exc}")
        return Response(
            {"error": "Invalid JSON payload.", "code": "invalid_json"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Schema Validation + Signature Check ────────────────────────────────
    is_valid, error_msg, cleaned_payload = validate_webhook_payload_signature_and_schema(
        payload,
        received_sig,
        verify_webhook_signature,
    )

    if not is_valid:
        logger.error(f"webhook_handler: validation failed: {error_msg}")
        # Route to dead-letter queue for manual review
        queue_dead_letter_event(payload, received_sig, error_msg)
        return Response(
            {"error": error_msg, "code": "validation_failed"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Event Dispatch ─────────────────────────────────────────────────────
    event_type = payload.get("event_type")
    org_id = payload.get("org_id")
    event_id = payload.get("event_id", f"{event_type}_{id(payload)}")

    try:
        result = get_billing_service().handle_webhook(
            event_type=event_type,
            payload=cleaned_payload,
            signature=received_sig,
            org_id=org_id,
        )
        return Response(result, status=status.HTTP_200_OK)

    except ValueError as exc:
        logger.error(f"webhook_handler: unsupported event type: {event_type}")
        queue_dead_letter_event(payload, received_sig, str(exc))
        return Response(
            {"error": str(exc), "code": "unsupported_event"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    except Exception as exc:
        logger.exception(f"webhook_handler: error processing event {event_id}: {exc}")
        return Response(
            {"error": "Internal error processing webhook.", "code": "internal_error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
