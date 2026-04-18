"""
Tests for POST /billing/webhooks — mock Stripe event handler.

Covers:
- Missing signature → 400
- Invalid signature → 400
- Valid payment_succeeded → marks invoice PAID + updates state
- Valid payment_failed → marks invoice FAILED, sub → past_due
- Valid subscription_canceled → marks subscription CANCELED
- Unknown event type → 400
"""

import hashlib
import hmac
import json

from django.conf import settings

import pytest
from rest_framework.test import APIClient

from tests.factories import InvoiceFactory, OrganizationFactory, PlanFactory, SubscriptionFactory

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/billing/webhooks/"


def _sign(payload: dict) -> str:
    """Generate a valid HMAC signature for the given payload dict."""
    body = json.dumps(payload).encode()
    secret = settings.SECRET_KEY.encode()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _post_webhook(client, payload: dict, sig: str = None, custom_body: bytes = None):
    """Post a webhook with optional custom body and signature."""
    body = custom_body if custom_body is not None else json.dumps(payload).encode()
    if sig is None:
        sig = _sign(payload) if custom_body is None else ""
    return client.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        HTTP_X_WEBHOOK_SIGNATURE=sig,
    )


class TestWebhookSecurity:
    def test_missing_signature_returns_400(self, client):
        payload = {"event_type": "payment_succeeded", "payload": {}}
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert response.status_code == 400
        assert response.data["code"] == "missing_signature"

    def test_invalid_signature_returns_400(self, client):
        payload = {"event_type": "payment_succeeded", "payload": {}}
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_WEBHOOK_SIGNATURE="invalidsig",
        )
        assert response.status_code == 400
        assert response.data["code"] == "invalid_signature"

    def test_tampered_payload_fails_signature_check(self, client):
        original = {"event_type": "payment_succeeded", "payload": {"invoice_id": "x"}}
        sig = _sign(original)
        tampered = {"event_type": "payment_failed", "payload": {"invoice_id": "x"}}
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(tampered),
            content_type="application/json",
            HTTP_X_WEBHOOK_SIGNATURE=sig,
        )
        assert response.status_code == 400

    def test_unknown_event_type_returns_400(self, client):
        payload = {"event_type": "unknown_event", "payload": {}}
        response = _post_webhook(client, payload)
        assert response.status_code == 400


class TestPaymentSucceeded:
    def test_marks_invoice_paid(self, client, db):
        from apps.billing.models import InvoiceStatus

        org = OrganizationFactory()
        plan = PlanFactory(slug="wh-pay-success", name="WH Pay Success")
        sub = SubscriptionFactory(organization=org, plan=plan)
        invoice = InvoiceFactory(subscription=sub, status="open", amount_cents=4900)

        payload = {
            "event_type": "payment_succeeded",
            "payload": {"invoice_id": invoice.stripe_invoice_id},
        }
        response = _post_webhook(client, payload)
        assert response.status_code == 200

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.paid_at is not None

    def test_missing_invoice_id_does_not_crash(self, client, db):
        payload = {"event_type": "payment_succeeded", "payload": {}}
        response = _post_webhook(client, payload)
        assert response.status_code == 200  # graceful handling

    def test_unknown_invoice_does_not_crash(self, client, db):
        payload = {
            "event_type": "payment_succeeded",
            "payload": {"invoice_id": "mock_inv_doesnotexist"},
        }
        response = _post_webhook(client, payload)
        assert response.status_code == 200  # graceful handling


class TestPaymentFailed:
    def test_marks_invoice_failed_and_sub_past_due(self, client, db):
        from apps.billing.models import InvoiceStatus, SubscriptionStatus

        org = OrganizationFactory()
        plan = PlanFactory(slug="wh-pay-fail", name="WH Pay Fail")
        sub = SubscriptionFactory(organization=org, plan=plan, status="active")
        invoice = InvoiceFactory(subscription=sub, status="open")

        payload = {
            "event_type": "payment_failed",
            "payload": {"invoice_id": invoice.stripe_invoice_id},
        }
        response = _post_webhook(client, payload)
        assert response.status_code == 200

        invoice.refresh_from_db()
        sub.refresh_from_db()
        assert invoice.status == InvoiceStatus.FAILED
        assert sub.status == SubscriptionStatus.PAST_DUE


class TestSubscriptionCanceled:
    def test_cancels_subscription(self, client, db):
        from apps.billing.models import SubscriptionStatus

        org = OrganizationFactory()
        plan = PlanFactory(slug="wh-sub-cancel", name="WH Sub Cancel")
        sub = SubscriptionFactory(organization=org, plan=plan, status="active")

        payload = {
            "event_type": "subscription_canceled",
            "payload": {"org_id": str(org.id)},
        }
        response = _post_webhook(client, payload)
        assert response.status_code == 200

        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.CANCELED
        assert sub.cancel_at is not None
