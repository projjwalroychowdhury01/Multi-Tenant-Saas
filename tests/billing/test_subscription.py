"""
Tests for subscription lifecycle:
  GET  /billing/subscription — view current subscription
  POST /billing/subscribe    — change plan (OWNER only)
"""

import pytest
from apps.tenants.models import RoleEnum
from tests.factories import (
    MembershipFactory,
    OrganizationFactory,
    PlanFactory,
    SubscriptionFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


def make_auth_client(api_client, user, org, role):
    """Helper: create membership + return authenticated client."""
    MembershipFactory(organization=org, user=user, role=role)
    res = api_client.post(
        "/auth/token",
        {"email": user.email, "password": "TestPass123!"},
        format="json",
    )
    assert res.status_code == 200
    token = res.data["access"]
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    api_client.org = org
    return api_client


class TestGetSubscription:
    URL = "/billing/subscription/"

    def test_owner_can_view_subscription(self, api_client, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="sub-view-pro", name="Pro View")
        sub = SubscriptionFactory(organization=org, plan=plan)
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.get(self.URL)
        assert response.status_code == 200
        assert response.data["status"] == "active"
        assert response.data["plan"]["slug"] == "sub-view-pro"

    def test_billing_role_can_view_subscription(self, api_client, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="sub-bill-role", name="Billing Role Plan")
        SubscriptionFactory(organization=org, plan=plan)
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.BILLING)

        response = client.get(self.URL)
        assert response.status_code == 200

    def test_member_cannot_view_subscription(self, api_client, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="sub-member-block", name="Member Block")
        SubscriptionFactory(organization=org, plan=plan)
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.MEMBER)

        response = client.get(self.URL)
        assert response.status_code == 403

    def test_no_subscription_returns_404(self, api_client, db):
        org = OrganizationFactory()
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.get(self.URL)
        assert response.status_code == 404

    def test_response_includes_grace_period_fields(self, api_client, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="sub-grace-fields", name="Grace Fields")
        SubscriptionFactory(organization=org, plan=plan)
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.get(self.URL)
        assert "is_in_grace_period" in response.data
        assert "grace_period_expired" in response.data
        assert response.data["is_in_grace_period"] is False


class TestSubscribe:
    URL = "/billing/subscribe/"

    def test_owner_can_subscribe(self, api_client, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="subscribe-pro", name="Pro Subscribe")
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.post(self.URL, {"plan_slug": "subscribe-pro"}, format="json")
        assert response.status_code == 200
        assert "subscription" in response.data
        assert response.data["subscription"]["plan"]["slug"] == "subscribe-pro"

    def test_billing_role_can_subscribe(self, api_client, db):
        """BILLING role has billing:manage so they can subscribe."""
        org = OrganizationFactory()
        PlanFactory(slug="subscribe-billing-role", name="Billing Role Sub")
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.BILLING)

        response = client.post(self.URL, {"plan_slug": "subscribe-billing-role"}, format="json")
        assert response.status_code == 200

    def test_admin_cannot_subscribe(self, api_client, db):
        """ADMIN doesn't have billing:manage."""
        org = OrganizationFactory()
        PlanFactory(slug="subscribe-admin-block", name="Admin Block")
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.ADMIN)

        response = client.post(self.URL, {"plan_slug": "subscribe-admin-block"}, format="json")
        assert response.status_code == 403

    def test_invalid_plan_slug_returns_400(self, api_client, db):
        org = OrganizationFactory()
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.post(self.URL, {"plan_slug": "nonexistent-plan"}, format="json")
        assert response.status_code == 400

    def test_subscribe_replaces_existing_subscription(self, api_client, db):
        org = OrganizationFactory()
        old_plan = PlanFactory(slug="old-sub-plan", name="Old Plan")
        new_plan = PlanFactory(slug="new-sub-plan", name="New Plan")
        SubscriptionFactory(organization=org, plan=old_plan)

        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.post(self.URL, {"plan_slug": "new-sub-plan"}, format="json")
        assert response.status_code == 200
        assert response.data["subscription"]["plan"]["slug"] == "new-sub-plan"

    def test_subscribe_creates_invoice(self, api_client, db):
        from apps.billing.models import Invoice
        org = OrganizationFactory()
        PlanFactory(slug="invoice-sub-plan", name="Invoice Plan", price_monthly="49.00")
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        client.post(self.URL, {"plan_slug": "invoice-sub-plan"}, format="json")
        assert Invoice.objects.filter(
            subscription__organization=org
        ).count() == 1

    def test_unauthenticated_cannot_subscribe(self, client, db):
        PlanFactory(slug="unauth-sub-plan", name="Unauth Plan")
        response = client.post(
            "/billing/subscribe/", {"plan_slug": "unauth-sub-plan"}, content_type="application/json"
        )
        assert response.status_code == 401
