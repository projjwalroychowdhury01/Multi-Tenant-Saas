"""
Cross-tenant isolation test suite — Phase 2 + Phase 3 + Phase 4.

These tests verify that Org A's authenticated tokens cannot read, mutate,
or delete Org B's resources through any API endpoint.

RULE: Every test in this file MUST assert HTTP 404 (not 403) to prevent
      confirming resource existence to a foreign tenant.  If a 403 is ever
      returned it means the resource was found but access was denied — which
      itself leaks information.

Phase 2 endpoints under test:
  GET    /orgs/<org_b_id>/members/
  PATCH  /orgs/<org_b_id>/members/<uid>/role/
  DELETE /orgs/<org_b_id>/members/<uid>/

Phase 3 endpoints under test:
  GET    /orgs/<org_b_id>/invitations/
  POST   /orgs/<org_b_id>/invitations/
  DELETE /orgs/<org_b_id>/invitations/<inv_id>/

Phase 4 (billing) isolation — subscription and invoice endpoints are
scoped to request.org, NOT to a URL org_id parameter, so cross-tenant
isolation is enforced by the TenantContextMiddleware rather than 404.
The tests confirm:
  - Org A client sees only Org A's subscription, not Org B's
  - Org A client sees only Org A's invoices, not Org B's
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.tenants.models import OrganizationInvitation, RoleEnum


@pytest.mark.django_db
class TestCrossTenantIsolation:
    """
    Cross-tenant isolation tests.
    Every assertion here is a hard security guarantee.
    """

    def setup_two_orgs(self):
        """
        Create two completely independent orgs each with an OWNER.
        Returns (org_a, org_b, user_a, user_b, client_a, client_b).
        """
        org_a = OrganizationFactory(name="Org Alpha")
        org_b = OrganizationFactory(name="Org Beta")

        user_a = UserFactory(password="TestPass123!")
        user_b = UserFactory(password="TestPass123!")

        MembershipFactory(organization=org_a, user=user_a, role=RoleEnum.OWNER)
        MembershipFactory(organization=org_b, user=user_b, role=RoleEnum.OWNER)

        client_a = self._make_auth_client(user_a.email, "TestPass123!")
        client_b = self._make_auth_client(user_b.email, "TestPass123!")

        # Create an extra member in Org B that Client A will try to target
        extra_b = UserFactory(password="TestPass123!")
        MembershipFactory(organization=org_b, user=extra_b, role=RoleEnum.MEMBER)

        return org_a, org_b, user_a, user_b, client_a, client_b, extra_b

    def _make_auth_client(self, email: str, password: str) -> APIClient:
        client = APIClient()
        res = client.post(
            "/auth/token",
            {"email": email, "password": password},
            format="json",
        )
        assert res.status_code == 200, f"Login failed for {email}: {res.data}"
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        return client

    # ── Sanity baseline ──────────────────────────────────────────────────────

    def test_setup_sanity(self):
        """Orgs and users are distinct — baseline sanity check."""
        org_a, org_b, user_a, user_b, *_ = self.setup_two_orgs()
        assert org_a.id != org_b.id
        assert user_a.id != user_b.id

    # ── List Members ─────────────────────────────────────────────────────────

    def test_list_members_foreign_org_returns_404(self):
        """
        Client A must not be able to list Org B's members.
        Expected: HTTP 404 (not 403 — the org's existence is opaque).
        """
        org_a, org_b, _, _, client_a, *_ = self.setup_two_orgs()
        res = client_a.get(f"/orgs/{org_b.id}/members/")
        assert (
            res.status_code == status.HTTP_404_NOT_FOUND
        ), f"Expected 404 but got {res.status_code}: {res.data}"

    def test_list_members_own_org_succeeds(self):
        """Control: Client A must still be able to list Org A's members."""
        org_a, _, _, _, client_a, *_ = self.setup_two_orgs()
        res = client_a.get(f"/orgs/{org_a.id}/members/")
        assert res.status_code == status.HTTP_200_OK

    # ── Change Role ──────────────────────────────────────────────────────────

    def test_change_role_foreign_member_returns_404(self):
        """
        Client A must not be able to change the role of Org B's member.
        Expected: HTTP 404 at the org resolution step.
        """
        org_a, org_b, _, _, client_a, _, extra_b = self.setup_two_orgs()
        res = client_a.patch(
            f"/orgs/{org_b.id}/members/{extra_b.id}/role/",
            {"role": RoleEnum.VIEWER},
            format="json",
        )
        assert (
            res.status_code == status.HTTP_404_NOT_FOUND
        ), f"Expected 404 but got {res.status_code}: {res.data}"

    def test_change_role_using_valid_org_but_foreign_user_returns_404(self):
        """
        Client A using their own org_id but targeting a user from Org B
        should return 404 (member not found in their org).
        """
        org_a, _, _, _, client_a, _, extra_b = self.setup_two_orgs()
        res = client_a.patch(
            f"/orgs/{org_a.id}/members/{extra_b.id}/role/",
            {"role": RoleEnum.VIEWER},
            format="json",
        )
        # extra_b is NOT a member of org_a → get_object_or_404 returns 404
        assert (
            res.status_code == status.HTTP_404_NOT_FOUND
        ), f"Expected 404 but got {res.status_code}: {res.data}"

    # ── Remove Member ────────────────────────────────────────────────────────

    def test_remove_foreign_member_returns_404(self):
        """
        Client A must not be able to DELETE Org B's member.
        Expected: HTTP 404 at the org resolution step.
        """
        org_a, org_b, _, _, client_a, _, extra_b = self.setup_two_orgs()
        res = client_a.delete(f"/orgs/{org_b.id}/members/{extra_b.id}/")
        assert (
            res.status_code == status.HTTP_404_NOT_FOUND
        ), f"Expected 404 but got {res.status_code}: {res.data}"

    def test_remove_using_valid_org_but_foreign_user_returns_404(self):
        """
        Client A using their own org_id but targeting a user from Org B
        should return 404.
        """
        org_a, _, _, _, client_a, _, extra_b = self.setup_two_orgs()
        res = client_a.delete(f"/orgs/{org_a.id}/members/{extra_b.id}/")
        assert (
            res.status_code == status.HTTP_404_NOT_FOUND
        ), f"Expected 404 but got {res.status_code}: {res.data}"

    # ── Unauthenticated ──────────────────────────────────────────────────────

    def test_unauthenticated_list_returns_401(self):
        """Unauthenticated requests must be rejected with 401."""
        org_a, _, _, _, *_ = self.setup_two_orgs()
        client = APIClient()  # no credentials
        res = client.get(f"/orgs/{org_a.id}/members/")
        assert res.status_code == status.HTTP_401_UNAUTHORIZED

    def test_unauthenticated_patch_returns_401(self):
        org_a, _, _, _, _, _, extra_b = self.setup_two_orgs()
        client = APIClient()
        res = client.patch(
            f"/orgs/{org_a.id}/members/{extra_b.id}/role/",
            {"role": RoleEnum.VIEWER},
            format="json",
        )
        assert res.status_code == status.HTTP_401_UNAUTHORIZED

    def test_unauthenticated_delete_returns_401(self):
        org_a, _, _, _, _, _, extra_b = self.setup_two_orgs()
        client = APIClient()
        res = client.delete(f"/orgs/{org_a.id}/members/{extra_b.id}/")
        assert res.status_code == status.HTTP_401_UNAUTHORIZED

    # ── Phase 3: Invitation Isolation ──────────────────────────────────────

    def test_cannot_list_foreign_org_invitations(self):
        """
        Client A requesting Org B's invitation list must get 404 —
        the org's existence is never confirmed to a non-member.
        """
        org_a, org_b, user_a, user_b, client_a, client_b, extra_b = self.setup_two_orgs()
        res = client_a.get(f"/orgs/{org_b.id}/invitations/")
        assert (
            res.status_code == status.HTTP_404_NOT_FOUND
        ), f"Expected 404 but got {res.status_code}: {res.data}"

    def test_cannot_create_invitation_in_foreign_org(self):
        """
        Client A cannot send an invitation into Org B.
        """
        org_a, org_b, user_a, user_b, client_a, client_b, extra_b = self.setup_two_orgs()
        res = client_a.post(
            f"/orgs/{org_b.id}/invitations/",
            {"email": "outsider@example.com", "role": RoleEnum.MEMBER},
            format="json",
        )
        assert (
            res.status_code == status.HTTP_404_NOT_FOUND
        ), f"Expected 404 but got {res.status_code}: {res.data}"

    def test_cannot_revoke_foreign_org_invitation(self):
        """
        Client A cannot revoke Org B's PENDING invitation even
        if they somehow obtain the correct invitation UUID.
        """
        org_a, org_b, _, user_b, client_a, client_b, extra_b = self.setup_two_orgs()

        # Create a real invite in Org B
        inv = OrganizationInvitation.objects.create(
            organization=org_b,
            email="victim@example.com",
            role=RoleEnum.MEMBER,
            invited_by=user_b,
        )

        # Client A (member of Org A) tries to revoke it using Org B's ID
        res = client_a.delete(f"/orgs/{org_b.id}/invitations/{inv.id}/")
        assert (
            res.status_code == status.HTTP_404_NOT_FOUND
        ), f"Expected 404 but got {res.status_code}: {res.data}"

    # ── Phase 4: Billing Isolation ────────────────────────────────────────────

    def test_subscription_endpoint_shows_own_org_only(self):
        """
        GET /billing/subscription/ must return Org A's subscription,
        NOT Org B's, regardless of the token used.

        Billing endpoints are scoped by request.org (from JWT claim),
        not by a URL org_id parameter.
        """
        from tests.factories import PlanFactory, SubscriptionFactory

        org_a, org_b, _, _, client_a, _, _ = self.setup_two_orgs()

        plan_a = PlanFactory(slug="iso-plan-a", name="Isolation Plan A", price_monthly="0.00")
        plan_b = PlanFactory(slug="iso-plan-b", name="Isolation Plan B", price_monthly="49.00")
        SubscriptionFactory(organization=org_a, plan=plan_a)
        SubscriptionFactory(organization=org_b, plan=plan_b)

        # Client A should see Org A's subscription (plan-a)
        res = client_a.get("/billing/subscription/")
        assert res.status_code == status.HTTP_200_OK
        assert res.data["plan"]["slug"] == "iso-plan-a", (
            f"Expected plan 'iso-plan-a' but got {res.data['plan']['slug']} — "
            f"cross-tenant bleed detected!"
        )

    def test_subscription_endpoint_cannot_access_org_b_data(self):
        """
        Client A's JWT is scoped to Org A. Even if Org B has a subscription,
        Client A must see a 404 if Org A has no subscription — not Org B's data.
        """
        from tests.factories import PlanFactory, SubscriptionFactory

        org_a, org_b, _, _, client_a, _, _ = self.setup_two_orgs()

        # Only Org B has a subscription
        plan_b = PlanFactory(slug="iso-only-b", name="Only Org B Plan", price_monthly="0.00")
        SubscriptionFactory(organization=org_b, plan=plan_b)

        # Client A (Org A) gets 404 — their org has no subscription
        res = client_a.get("/billing/subscription/")
        assert res.status_code == status.HTTP_404_NOT_FOUND, (
            f"Expected 404 for Org A (no sub) but got {res.status_code} — "
            f"possible cross-tenant bleed to Org B's data!"
        )

    def test_invoices_endpoint_shows_own_org_only(self):
        """
        GET /billing/invoices/ returns only invoices for the authenticated
        user's org, never from another tenant's subscription.
        """
        from tests.factories import InvoiceFactory, PlanFactory, SubscriptionFactory

        org_a, org_b, _, _, client_a, _, _ = self.setup_two_orgs()

        plan_a = PlanFactory(slug="iso-inv-a", name="Invoice Iso A", price_monthly="0.00")
        plan_b = PlanFactory(slug="iso-inv-b", name="Invoice Iso B", price_monthly="49.00")
        sub_a = SubscriptionFactory(organization=org_a, plan=plan_a)
        sub_b = SubscriptionFactory(organization=org_b, plan=plan_b)
        InvoiceFactory(subscription=sub_a, amount_cents=100)
        InvoiceFactory(subscription=sub_b, amount_cents=4900)

        res = client_a.get("/billing/invoices/")
        assert res.status_code == status.HTTP_200_OK
        # Client A should see exactly 1 invoice (their own), not Org B's
        assert len(res.data) == 1, (
            f"Expected 1 invoice for Org A but got {len(res.data)} — "
            f"possible cross-tenant bleed!"
        )
        assert (
            res.data[0]["amount_cents"] == 100
        ), f"Invoice amount mismatch — cross-tenant invoice returned!"
