"""
BillingService — abstract interface + MockBillingService implementation.

Design
──────
The service layer is intentionally isolated from views. Views call the
service; the service manages all state transitions and side effects.

Swapping in real Stripe later requires only:
  1. Write a ``StripeBillingService(BillingService)`` subclass
  2. Change the ``get_billing_service()`` factory to return it

The mock implementation is complete enough to exercise the full billing
lifecycle in tests and demos without a real Stripe account.
"""

import hashlib
import hmac
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Abstract Interface ─────────────────────────────────────────────────────────


class BillingService(ABC):
    """
    Contract that every billing back-end must satisfy.

    All methods receive/return plain Python objects (no HTTP request objects)
    so they are easy to test in isolation.
    """

    @abstractmethod
    def subscribe(self, org, plan):
        """
        Activate or change the subscription for *org* to *plan*.

        If the org already has a subscription, it is replaced:
        the old subscription is cancelled and a new one is created.
        Returns the new Subscription instance.
        """

    @abstractmethod
    def cancel(self, org):
        """
        Cancel the org's current subscription immediately.
        Sets status='canceled' and cancel_at=now().
        Returns the updated Subscription.
        """

    @abstractmethod
    def handle_webhook(self, event_type: str, payload: dict):
        """
        Dispatch an incoming webhook event to the appropriate handler.

        Raises ValueError for unknown event types.
        Returns a dict with ``{"processed": True, "event": event_type}``.
        """


# ── Mock Implementation ────────────────────────────────────────────────────────


class MockBillingService(BillingService):
    """
    In-memory mock that satisfies the BillingService contract.

    - Creates real DB rows (Subscription, Invoice) so integration tests
      can assert against the database state.
    - Does NOT call external APIs.
    - Webhook signature verification uses Django's SECRET_KEY as the HMAC
      secret — real Stripe would use the webhook signing secret.
    """

    SUPPORTED_EVENTS = frozenset(
        [
            "payment_succeeded",
            "payment_failed",
            "subscription_canceled",
        ]
    )

    def subscribe(self, org, plan):
        from apps.billing.models import Invoice, InvoiceStatus, Subscription, SubscriptionStatus

        now = timezone.now()
        period_end = now + timedelta(days=30)

        # Cancel existing subscription if one exists
        try:
            old_sub = Subscription.objects.get(organization=org)
            if old_sub.status != SubscriptionStatus.CANCELED:
                logger.info(
                    "billing.subscribe: replacing existing subscription %s for org %s",
                    old_sub.id,
                    org.id,
                )
            # Delete the old subscription so the OneToOne constraint allows a new one.
            # The invoices cascade-delete with it — they are already paid or failed.
            # In production Stripe keeps the history; the mock keeps it simple.
            old_sub.delete()
        except Subscription.DoesNotExist:
            pass

        # Create new subscription
        subscription = Subscription.objects.create(
            organization=org,
            plan=plan,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now,
            current_period_end=period_end,
        )

        # Sync org.billing_plan FK to the new plan so rate-limiting and
        # feature-gate helpers see the correct tier without an extra join.
        org.billing_plan = plan
        org.save(update_fields=["billing_plan", "updated_at"])

        # Issue an opening invoice
        amount_cents = int(plan.price_monthly * 100)
        Invoice.objects.create(
            subscription=subscription,
            amount_cents=amount_cents,
            status=InvoiceStatus.PAID if amount_cents == 0 else InvoiceStatus.OPEN,
            stripe_invoice_id=f"mock_inv_{uuid.uuid4().hex[:12]}",
            period_start=now,
            period_end=period_end,
            paid_at=now if amount_cents == 0 else None,
        )

        logger.info(
            "billing.subscribe: org %s subscribed to plan %s (sub %s)",
            org.id,
            plan.slug,
            subscription.id,
        )
        return subscription

    def cancel(self, org):
        from apps.billing.models import Subscription, SubscriptionStatus

        subscription = Subscription.objects.get(organization=org)
        subscription.status = SubscriptionStatus.CANCELED
        subscription.cancel_at = timezone.now()
        subscription.save(update_fields=["status", "cancel_at", "updated_at"])

        logger.info(
            "billing.cancel: subscription %s canceled for org %s",
            subscription.id,
            org.id,
        )
        return subscription

    def handle_webhook(self, event_type: str, payload: dict, signature: str = "", org_id = None):
        """
        Handle webhook event with validation and replay protection.
        
        Args:
            event_type: Type of event (payment_succeeded, etc.)
            payload: Event payload dict
            signature: HMAC signature for verification
            org_id: Organization ID (optional, extracted from payload if needed)
        
        Raises:
            ValueError for unsupported events or validation failures
        """
        from apps.billing.models import WebhookEvent, WebhookEventStatus
        from apps.billing.webhook_validation import (
            validate_webhook_event,
            queue_dead_letter_event,
            WebhookValidationError,
        )
        
        if event_type not in self.SUPPORTED_EVENTS:
            raise ValueError(f"Unsupported webhook event: {event_type!r}")

        # Extract event_id for replay protection
        event_id = payload.get("event_id", f"{event_type}_{uuid.uuid4().hex}")

        # Check for duplicate event (replay protection)
        if WebhookEvent.objects.filter(event_id=event_id).exists():
            logger.warning(f"Webhook event {event_id} already processed (replay detected)")
            # Return success to avoid webhook retry storms
            return {"processed": True, "event": event_type, "cached": True}

        # Validate webhook payload schema
        try:
            cleaned_payload = validate_webhook_event(event_type, payload)
        except WebhookValidationError as exc:
            logger.error(f"Webhook validation failed for {event_type}: {exc}")
            queue_dead_letter_event(payload, signature, str(exc))
            raise

        # Determine organization from payload
        if org_id is None:
            org_id = payload.get("org_id")

        # Record webhook event
        webhook_event = WebhookEvent.objects.create(
            event_id=event_id,
            event_type=event_type,
            status=WebhookEventStatus.PENDING,
            payload=payload,
            signature=signature,
            organization_id=org_id,
        )

        try:
            # Dispatch to handler
            handler = {
                "payment_succeeded": self._on_payment_succeeded,
                "payment_failed": self._on_payment_failed,
                "subscription_canceled": self._on_subscription_canceled,
            }[event_type]

            handler(cleaned_payload)

            # Mark as processed
            webhook_event.status = WebhookEventStatus.PROCESSED
            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["status", "processed_at"])

            logger.info(f"Webhook {event_id} processed successfully")
            return {"processed": True, "event": event_type}

        except Exception as exc:
            logger.exception(f"Error processing webhook {event_id}: {exc}")
            webhook_event.status = WebhookEventStatus.FAILED
            webhook_event.error_message = str(exc)
            webhook_event.retry_count = webhook_event.retry_count + 1
            webhook_event.save(update_fields=["status", "error_message", "retry_count"])
            raise

    # ── Private event handlers ─────────────────────────────────────────────────

    def _on_payment_succeeded(self, payload: dict):
        """Mark invoice as paid and dispatch the invoice email task."""
        from apps.billing.models import Invoice, InvoiceStatus

        invoice_id = payload.get("invoice_id")
        if not invoice_id:
            logger.warning("payment_succeeded: missing invoice_id in payload")
            return

        try:
            invoice = Invoice.objects.get(stripe_invoice_id=invoice_id)
        except Invoice.DoesNotExist:
            logger.warning("payment_succeeded: invoice %s not found", invoice_id)
            return

        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = timezone.now()
        invoice.save(update_fields=["status", "paid_at", "updated_at"])

        # Fire-and-forget email — errors are caught so the webhook still succeeds
        try:
            from apps.billing.tasks import send_invoice_email
            send_invoice_email.delay(str(invoice.id))
        except Exception as exc:
            logger.warning("payment_succeeded: could not enqueue email task: %s", exc)

        logger.info("payment_succeeded: invoice %s marked paid", invoice_id)

    def _on_payment_failed(self, payload: dict):
        """Mark invoice as failed and set subscription to past_due."""
        from apps.billing.models import Invoice, InvoiceStatus, Subscription, SubscriptionStatus

        invoice_id = payload.get("invoice_id")
        if not invoice_id:
            logger.warning("payment_failed: missing invoice_id in payload")
            return

        try:
            invoice = Invoice.objects.get(stripe_invoice_id=invoice_id)
        except Invoice.DoesNotExist:
            logger.warning("payment_failed: invoice %s not found", invoice_id)
            return

        invoice.status = InvoiceStatus.FAILED
        invoice.save(update_fields=["status", "updated_at"])

        # Mark subscription as past_due
        sub = invoice.subscription
        if sub.status == SubscriptionStatus.ACTIVE:
            sub.status = SubscriptionStatus.PAST_DUE
            sub.save(update_fields=["status", "updated_at"])

        logger.info("payment_failed: invoice %s failed, sub %s → past_due", invoice_id, sub.id)

    def _on_subscription_canceled(self, payload: dict):
        """Cancel an org's subscription from an external event."""
        from apps.billing.models import Subscription, SubscriptionStatus

        org_id = payload.get("org_id")
        if not org_id:
            logger.warning("subscription_canceled: missing org_id in payload")
            return

        try:
            sub = Subscription.objects.get(organization_id=org_id)
        except Subscription.DoesNotExist:
            logger.warning("subscription_canceled: no subscription for org %s", org_id)
            return

        sub.status = SubscriptionStatus.CANCELED
        sub.cancel_at = timezone.now()
        sub.save(update_fields=["status", "cancel_at", "updated_at"])
        logger.info("subscription_canceled: sub %s for org %s canceled", sub.id, org_id)


# ── Webhook Signature Verification ────────────────────────────────────────────


def verify_webhook_signature(payload_bytes: bytes, received_sig: str) -> bool:
    """
    Verify a mock Stripe-style webhook signature.

    Uses HMAC-SHA256 with Django's SECRET_KEY as the signing secret.
    Real Stripe uses its own ``whsec_...`` secret — swap this function
    when wiring up the real Stripe integration.

    Args:
        payload_bytes: Raw request body bytes.
        received_sig:  Value of the ``X-Webhook-Signature`` header.

    Returns:
        True if the signature is valid, False otherwise.
    """
    secret = settings.SECRET_KEY.encode()
    expected = hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()
    # Constant-time comparison to resist timing attacks
    return hmac.compare_digest(expected, received_sig)


# ── Service Factory ────────────────────────────────────────────────────────────


_billing_service: BillingService | None = None


def get_billing_service() -> BillingService:
    """
    Return the configured BillingService singleton.

    Uses MockBillingService by default. Override ``BILLING_BACKEND`` in
    settings to point at a real implementation::

        BILLING_BACKEND = "apps.billing.stripe_service.StripeBillingService"
    """
    global _billing_service
    if _billing_service is None:
        backend = getattr(settings, "BILLING_BACKEND", None)
        if backend:
            from django.utils.module_loading import import_string
            _billing_service = import_string(backend)()
        else:
            _billing_service = MockBillingService()
    return _billing_service
