"""
Cross-tenant isolation test suite — Phase 2 + Phase 3.

These tests verify that Org A's authenticated tokens cannot read, mutate,
or delete Org B's members or invitations through any API endpoint.

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
        assert res.status_code == status.HTTP_404_NOT_FOUND, (
            f"Expected 404 but got {res.status_code}: {res.data}"
        )

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
        assert res.status_code == status.HTTP_404_NOT_FOUND, (
            f"Expected 404 but got {res.status_code}: {res.data}"
        )

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
        assert res.status_code == status.HTTP_404_NOT_FOUND, (
            f"Expected 404 but got {res.status_code}: {res.data}"
        )

    # ── Remove Member ────────────────────────────────────────────────────────

    def test_remove_foreign_member_returns_404(self):
        """
        Client A must not be able to DELETE Org B's member.
        Expected: HTTP 404 at the org resolution step.
        """
        org_a, org_b, _, _, client_a, _, extra_b = self.setup_two_orgs()
        res = client_a.delete(f"/orgs/{org_b.id}/members/{extra_b.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND, (
            f"Expected 404 but got {res.status_code}: {res.data}"
        )

    def test_remove_using_valid_org_but_foreign_user_returns_404(self):
        """
        Client A using their own org_id but targeting a user from Org B
        should return 404.
        """
        org_a, _, _, _, client_a, _, extra_b = self.setup_two_orgs()
        res = client_a.delete(f"/orgs/{org_a.id}/members/{extra_b.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND, (
            f"Expected 404 but got {res.status_code}: {res.data}"
        )

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
        assert res.status_code == status.HTTP_404_NOT_FOUND, (
            f"Expected 404 but got {res.status_code}: {res.data}"
        )

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
        assert res.status_code == status.HTTP_404_NOT_FOUND, (
            f"Expected 404 but got {res.status_code}: {res.data}"
        )

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
        assert res.status_code == status.HTTP_404_NOT_FOUND, (
            f"Expected 404 but got {res.status_code}: {res.data}"
        )
