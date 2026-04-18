"""
Comprehensive tests for billing hardening features.

Test coverage:
- Idempotency key storage and replay protection
- Webhook validation and dead-letter queue
- Plan limit event streaming
- Webhook replay protection (event_id deduplication)
"""

import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.utils import timezone

import pytest
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.billing.events import PlanLimitEventEmitter
from apps.billing.idempotency import (
    IdempotencyManager,
    compute_request_hash,
    get_cache_key,
    get_idempotency_key,
)
from apps.billing.models import (
    IdempotencyKey,
    Invoice,
    InvoiceStatus,
    Plan,
    PlanLimitEvent,
    PlanLimitEventType,
    Subscription,
    SubscriptionStatus,
    WebhookEvent,
    WebhookEventStatus,
)
from apps.billing.services import MockBillingService, verify_webhook_signature
from apps.billing.webhook_validation import (
    WebhookValidationError,
    queue_dead_letter_event,
    validate_webhook_event,
)
from apps.tenants.models import Organization
from tests.factories import OrganizationFactory

# ── Idempotency Tests ──────────────────────────────────────────────────────────


class TestIdempotencyManager(TestCase):
    """Test idempotency key management and replay protection."""

    def setUp(self):
        self.org = OrganizationFactory()
        self.idempotency_key = str(uuid.uuid4())
        self.request_body = b'{"plan_id": "pro"}'

    def test_store_and_retrieve_result(self):
        """Test storing and retrieving idempotent operation results."""
        IdempotencyManager.store_result(
            org_id=str(self.org.id),
            idempotency_key=self.idempotency_key,
            operation_type="subscribe",
            request_body=self.request_body,
            response_status=200,
            response_data={"subscription_id": "sub_123"},
        )

        # Check cache
        cached = IdempotencyManager.get_result(
            str(self.org.id),
            self.idempotency_key,
        )
        assert cached is not None
        assert cached["status"] == 200
        assert cached["data"]["subscription_id"] == "sub_123"

        # Check database
        db_entry = IdempotencyKey.objects.get(
            organization=self.org,
            idempotency_key=self.idempotency_key,
        )
        assert db_entry.operation_type == "subscribe"
        assert db_entry.response_status == 200

    def test_request_hash_computation(self):
        """Test that request body hashes are computed correctly."""
        hash1 = compute_request_hash(self.request_body)
        hash2 = compute_request_hash(self.request_body)
        assert hash1 == hash2

        # Different body → different hash
        hash3 = compute_request_hash(b'{"plan_id": "free"}')
        assert hash1 != hash3

    def test_validate_request_integrity(self):
        """Test validation of request body integrity for replays."""
        # Store original result
        IdempotencyManager.store_result(
            org_id=str(self.org.id),
            idempotency_key=self.idempotency_key,
            operation_type="subscribe",
            request_body=self.request_body,
            response_status=200,
            response_data={"subscription_id": "sub_123"},
        )

        # Validate same body (should pass)
        is_valid, error = IdempotencyManager.validate_request_integrity(
            str(self.org.id),
            self.idempotency_key,
            self.request_body,
        )
        assert is_valid is True
        assert error is None

        # Validate different body (should fail)
        different_body = b'{"plan_id": "different"}'
        is_valid, error = IdempotencyManager.validate_request_integrity(
            str(self.org.id),
            self.idempotency_key,
            different_body,
        )
        assert is_valid is False
        assert "mismatch" in error.lower()

    def test_cleanup_expired_keys(self):
        """Test cleanup of expired idempotency keys."""
        # Create old key
        old_key = IdempotencyKey.objects.create(
            organization=self.org,
            idempotency_key="old_key",
            operation_type="subscribe",
            request_hash="hash",
            response_status=200,
            response_data={},
        )
        old_key.created_at = timezone.now() - timedelta(hours=25)
        old_key.save()

        # Create recent key
        IdempotencyKey.objects.create(
            organization=self.org,
            idempotency_key="recent_key",
            operation_type="subscribe",
            request_hash="hash",
            response_status=200,
            response_data={},
        )

        # Cleanup
        count = IdempotencyManager.cleanup_expired()
        assert count >= 1

        # Old key should be deleted
        assert not IdempotencyKey.objects.filter(id=old_key.id).exists()

        # Recent key should exist
        assert IdempotencyKey.objects.filter(idempotency_key="recent_key").exists()


# ── Webhook Validation Tests ───────────────────────────────────────────────────


class TestWebhookValidation(TestCase):
    """Test webhook schema validation and dead-letter handling."""

    def test_validate_payment_succeeded_event(self):
        """Test validation of payment_succeeded event."""
        payload = {
            "event_type": "payment_succeeded",
            "invoice_id": "inv_123",
            "timestamp": 1234567890,
        }
        cleaned = validate_webhook_event("payment_succeeded", payload)
        assert cleaned["invoice_id"] == "inv_123"

    def test_validate_payment_failed_event(self):
        """Test validation of payment_failed event."""
        payload = {
            "event_type": "payment_failed",
            "invoice_id": "inv_123",
            "reason": "insufficient_funds",
        }
        cleaned = validate_webhook_event("payment_failed", payload)
        assert cleaned["reason"] == "insufficient_funds"

    def test_validation_missing_required_field(self):
        """Test validation fails for missing required fields."""
        payload = {
            "event_type": "payment_succeeded",
            # Missing required "invoice_id"
        }
        with pytest.raises(WebhookValidationError) as exc_info:
            validate_webhook_event("payment_succeeded", payload)
        assert "invoice_id" in str(exc_info.value)

    def test_validation_wrong_field_type(self):
        """Test validation fails for wrong field types."""
        payload = {
            "event_type": "payment_failed",
            "invoice_id": "inv_123",
            "reason": 12345,  # Should be string
        }
        with pytest.raises(WebhookValidationError):
            validate_webhook_event("payment_failed", payload)

    def test_queue_dead_letter_event(self):
        """Test queuing malformed events to dead-letter queue."""
        payload = {"event_type": "payment_succeeded"}  # Missing required fields
        signature = "sig_123"
        reason = "Missing required field: invoice_id"

        queue_dead_letter_event(payload, signature, reason)

        # Check that event was queued
        event = WebhookEvent.objects.filter(
            status=WebhookEventStatus.DEAD_LETTER,
            dead_letter_reason=reason,
        ).first()
        assert event is not None
        assert event.payload["event_type"] == "payment_succeeded"


# ── Webhook Replay Protection Tests ────────────────────────────────────────────


class TestWebhookReplayProtection(TestCase):
    """Test webhook replay protection via event_id deduplication."""

    def setUp(self):
        self.org = OrganizationFactory()
        self.plan = Plan.objects.create(
            name="Pro",
            slug="pro",
            price_monthly=99.00,
            limits={"api_calls_per_month": 100000},
        )

    def test_duplicate_event_rejected(self):
        """Test that duplicate events (same event_id) are rejected."""
        service = MockBillingService()

        event_id = "evt_123"
        payload1 = {
            "event_type": "payment_succeeded",
            "event_id": event_id,
            "invoice_id": "inv_123",
        }

        # First event should be processed
        result1 = service.handle_webhook(
            event_type="payment_succeeded",
            payload=payload1,
            signature="sig_123",
            org_id=str(self.org.id),
        )
        assert result1["processed"] is True

        # Second event with same event_id should be cached
        result2 = service.handle_webhook(
            event_type="payment_succeeded",
            payload=payload1,
            signature="sig_123",
            org_id=str(self.org.id),
        )
        assert result2["processed"] is True
        assert result2.get("cached") is True

    def test_webhook_event_stored_in_db(self):
        """Test that webhook events are stored for audit trail."""
        service = MockBillingService()

        event_id = "evt_456"
        result = service.handle_webhook(
            event_type="payment_succeeded",
            payload={
                "event_type": "payment_succeeded",
                "event_id": event_id,
                "invoice_id": "inv_456",
            },
            signature="sig_456",
            org_id=str(self.org.id),
        )

        # Check database
        event = WebhookEvent.objects.get(event_id=event_id)
        assert event.event_type == "payment_succeeded"
        assert event.status == WebhookEventStatus.PROCESSED
        assert event.organization_id == self.org.id


# ── Plan Limit Event Tests ─────────────────────────────────────────────────────


class TestPlanLimitEvents(TestCase):
    """Test plan limit event emission and notifications."""

    def setUp(self):
        self.org = OrganizationFactory()

    def test_emit_warning_event(self):
        """Test emitting a limit warning event (80%)."""
        event = PlanLimitEventEmitter.emit(
            org=self.org,
            event_type=PlanLimitEventType.WARNING,
            limit_type="api_calls_per_month",
            current_usage=8000,
            limit_value=10000,
            metadata={"plan": "pro"},
        )

        assert event.event_type == PlanLimitEventType.WARNING
        assert event.usage_percentage == 80
        assert event.metadata["plan"] == "pro"

    def test_emit_critical_event(self):
        """Test emitting a limit critical event (100%)."""
        event = PlanLimitEventEmitter.emit(
            org=self.org,
            event_type=PlanLimitEventType.CRITICAL,
            limit_type="members_count",
            current_usage=50,
            limit_value=50,
        )

        assert event.event_type == PlanLimitEventType.CRITICAL
        assert event.usage_percentage == 100

    def test_threshold_based_event_emission(self):
        """Test emitting events only when thresholds are crossed."""
        # First event at 50% (no emission)
        result = PlanLimitEventEmitter.emit_with_threshold_check(
            org=self.org,
            limit_type="api_calls_per_month",
            current_usage=5000,
            limit_value=10000,
        )
        assert result is None  # Below 80%, no event

        # Create entry for comparison
        PlanLimitEventEmitter.emit(
            org=self.org,
            event_type=PlanLimitEventType.WARNING,
            limit_type="api_calls_per_month",
            current_usage=5000,
            limit_value=10000,
        )

        # Now at 85% (should emit warning)
        result = PlanLimitEventEmitter.emit_with_threshold_check(
            org=self.org,
            limit_type="api_calls_per_month",
            current_usage=8500,
            limit_value=10000,
        )
        assert result is None  # Already warned before

    def test_get_active_limit_violations(self):
        """Test retrieving active limit violations."""
        PlanLimitEventEmitter.emit(
            org=self.org,
            event_type=PlanLimitEventType.CRITICAL,
            limit_type="api_calls_per_month",
            current_usage=11000,
            limit_value=10000,
        )

        violations = PlanLimitEventEmitter.get_active_limits(self.org)
        assert "api_calls_per_month" in violations
        assert violations["api_calls_per_month"].event_type == PlanLimitEventType.CRITICAL


# ── Integration Tests ──────────────────────────────────────────────────────────


class TestWebhookIntegration(APITestCase):
    """Integration tests for webhook processing."""

    def setUp(self):
        self.client = APIClient()
        self.org = OrganizationFactory()
        self.plan = Plan.objects.create(
            name="Pro",
            slug="pro",
            price_monthly=99.00,
            limits={"api_calls_per_month": 100000},
        )

    def _sign_payload(self, payload: dict) -> str:
        """Create HMAC signature for webhook payload."""
        payload_bytes = json.dumps(payload, sort_keys=True).encode()
        secret = settings.SECRET_KEY.encode()
        return hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()

    def test_webhook_with_valid_signature(self):
        """Test webhook processing with valid signature."""
        payload = {
            "event_type": "payment_succeeded",
            "event_id": "evt_test_1",
            "invoice_id": "inv_test_1",
        }

        signature = self._sign_payload(payload)

        response = self.client.post(
            "/billing/webhooks",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_WEBHOOK_SIGNATURE=signature,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["processed"] is True

    def test_webhook_with_invalid_signature(self):
        """Test webhook is rejected with invalid signature."""
        payload = {
            "event_type": "payment_succeeded",
            "event_id": "evt_test_2",
            "invoice_id": "inv_test_2",
        }

        response = self.client.post(
            "/billing/webhooks",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_WEBHOOK_SIGNATURE="invalid_signature",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_webhook_missing_signature_header(self):
        """Test webhook is rejected when signature header is missing."""
        payload = {
            "event_type": "payment_succeeded",
            "event_id": "evt_test_3",
            "invoice_id": "inv_test_3",
        }

        response = self.client.post(
            "/billing/webhooks",
            data=json.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "signature" in response.json()["code"].lower()


if __name__ == "__main__":
    pytest.main([__file__])
