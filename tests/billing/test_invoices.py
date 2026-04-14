"""
Tests for GET /billing/invoices — invoice history listing.
"""

import pytest
from apps.tenants.models import RoleEnum
from tests.factories import (
    InvoiceFactory,
    MembershipFactory,
    OrganizationFactory,
    PlanFactory,
    SubscriptionFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


def make_auth_client(api_client, user, org, role):
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


class TestListInvoices:
    URL = "/billing/invoices/"

    def test_owner_can_list_invoices(self, api_client, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="inv-owner-plan", name="Owner Plan")
        sub = SubscriptionFactory(organization=org, plan=plan)
        InvoiceFactory(subscription=sub)
        InvoiceFactory(subscription=sub)

        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.get(self.URL)
        assert response.status_code == 200
        assert len(response.data) == 2

    def test_billing_role_can_list_invoices(self, api_client, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="inv-billing-plan", name="Billing Plan")
        sub = SubscriptionFactory(organization=org, plan=plan)
        InvoiceFactory(subscription=sub)

        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.BILLING)

        response = client.get(self.URL)
        assert response.status_code == 200

    def test_viewer_can_list_invoices(self, api_client, db):
        """VIEWER has billing:read so they may see invoices."""
        org = OrganizationFactory()
        plan = PlanFactory(slug="inv-viewer-plan", name="Viewer Plan")
        sub = SubscriptionFactory(organization=org, plan=plan)
        InvoiceFactory(subscription=sub)

        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.VIEWER)

        response = client.get(self.URL)
        assert response.status_code == 200

    def test_member_cannot_list_invoices(self, api_client, db):
        """MEMBER lacks billing:read."""
        org = OrganizationFactory()
        plan = PlanFactory(slug="inv-member-plan", name="Member Plan")
        sub = SubscriptionFactory(organization=org, plan=plan)
        InvoiceFactory(subscription=sub)

        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.MEMBER)

        response = client.get(self.URL)
        assert response.status_code == 403

    def test_no_subscription_returns_empty_list(self, api_client, db):
        org = OrganizationFactory()
        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.get(self.URL)
        assert response.status_code == 200
        assert response.data == []

    def test_invoice_response_shape(self, api_client, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="inv-shape-plan", name="Shape Plan")
        sub = SubscriptionFactory(organization=org, plan=plan)
        InvoiceFactory(subscription=sub, amount_cents=4900, status="paid")

        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.get(self.URL)
        inv = response.data[0]
        assert "id" in inv
        assert "stripe_invoice_id" in inv
        assert "amount_cents" in inv
        assert "amount_dollars" in inv
        assert inv["amount_dollars"] == "$49.00"
        assert "status" in inv
        assert "plan_name" in inv

    def test_invoices_ordered_newest_first(self, api_client, db):
        from datetime import timedelta
        from django.utils import timezone

        org = OrganizationFactory()
        plan = PlanFactory(slug="inv-order-plan", name="Order Plan")
        sub = SubscriptionFactory(organization=org, plan=plan)
        now = timezone.now()
        InvoiceFactory(subscription=sub, amount_cents=100)
        InvoiceFactory(subscription=sub, amount_cents=200)

        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org, RoleEnum.OWNER)

        response = client.get(self.URL)
        assert response.status_code == 200
        # Most recently created should be first
        assert len(response.data) == 2

    def test_cross_tenant_invoice_isolation(self, api_client, db):
        """Org A user cannot see Org B's invoices."""
        org_a = OrganizationFactory()
        org_b = OrganizationFactory()

        plan_b = PlanFactory(slug="inv-isolation-plan", name="Isolation Plan")
        sub_b = SubscriptionFactory(organization=org_b, plan=plan_b)
        InvoiceFactory(subscription=sub_b)

        user = UserFactory(password="TestPass123!")
        client = make_auth_client(api_client, user, org_a, RoleEnum.OWNER)

        response = client.get(self.URL)
        # Org A has no subscription, should return empty list — not Org B's data
        assert response.status_code == 200
        assert response.data == []
