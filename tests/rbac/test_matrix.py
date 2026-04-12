"""
Role × Endpoint matrix test — hard CI gate.

For each of the 5 roles (OWNER, ADMIN, MEMBER, VIEWER, BILLING) this file
exercises every Phase 2 endpoint and asserts the correct HTTP status code.

Matrix columns (endpoints):
  A) GET  /auth/me/permissions
  B) GET  /orgs/<id>/members/
  C) PATCH /orgs/<id>/members/<uid>/role/   — role change
  D) DELETE /orgs/<id>/members/<uid>/       — removal

Expected access:
  ┌─────────┬───┬───┬───┬───┐
  │ Role    │ A │ B │ C │ D │
  ├─────────┼───┼───┼───┼───┤
  │ OWNER   │200│200│200│204│
  │ ADMIN   │200│200│200│204│
  │ MEMBER  │200│200│403│403│
  │ VIEWER  │200│200│403│403│
  │ BILLING │200│403│403│403│
  └─────────┴───┴───┴───┴───┘

Notes:
  - Column A (me/permissions) is always 200 for any authenticated user.
  - BILLING has no users:read → 403 on member list.
  - MEMBER / VIEWER have users:read → 200 on list, but no users:manage → 403 on mutations.
  - OWNER and ADMIN both get 200 / 204 on mutations.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.tenants.models import RoleEnum


# Helper roles to iterate over for parametrize
ALL_ROLES = [
    RoleEnum.OWNER,
    RoleEnum.ADMIN,
    RoleEnum.MEMBER,
    RoleEnum.VIEWER,
    RoleEnum.BILLING,
]


def _make_client_for_role(role: str, org):
    """Create a user with *role* in *org*, return an authenticated APIClient."""
    user = UserFactory(password="TestPass123!")
    MembershipFactory(organization=org, user=user, role=role)
    client = APIClient()
    res = client.post(
        "/auth/token",
        {"email": user.email, "password": "TestPass123!"},
        format="json",
    )
    assert res.status_code == 200, f"Login failed for {role}: {res.data}"
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
    client._user = user
    return client


@pytest.mark.django_db
class TestPermissionsEndpointForAllRoles:
    """Column A — GET /auth/me/permissions must succeed for every role."""

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_me_permissions_returns_200(self, role):
        org = OrganizationFactory()
        client = _make_client_for_role(role, org)
        res = client.get("/auth/me/permissions")
        assert res.status_code == status.HTTP_200_OK
        assert "role" in res.data
        assert "permissions" in res.data
        assert isinstance(res.data["permissions"], list)
        assert res.data["role"] == role


@pytest.mark.django_db
class TestMemberListMatrix:
    """Column B — GET /orgs/<id>/members/"""

    # Roles that have users:read → expect 200
    @pytest.mark.parametrize("role", [
        RoleEnum.OWNER,
        RoleEnum.ADMIN,
        RoleEnum.MEMBER,
        RoleEnum.VIEWER,
    ])
    def test_allowed_roles_see_members(self, role):
        org = OrganizationFactory()
        client = _make_client_for_role(role, org)
        res = client.get(f"/orgs/{org.id}/members/")
        assert res.status_code == status.HTTP_200_OK
        assert "results" in res.data

    # BILLING has no users:read → expect 403
    def test_billing_denied_member_list(self):
        org = OrganizationFactory()
        client = _make_client_for_role(RoleEnum.BILLING, org)
        res = client.get(f"/orgs/{org.id}/members/")
        assert res.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestChangeRoleMatrix:
    """Column C — PATCH /orgs/<id>/members/<uid>/role/"""

    def _setup_target(self, org, target_role=RoleEnum.MEMBER):
        """Create a target MEMBER in the org to act upon."""
        target = UserFactory(password="TestPass123!")
        MembershipFactory(organization=org, user=target, role=target_role)
        return target

    @pytest.mark.parametrize("actor_role", [RoleEnum.OWNER, RoleEnum.ADMIN])
    def test_admin_and_owner_can_change_role(self, actor_role):
        org = OrganizationFactory()
        client = _make_client_for_role(actor_role, org)
        target = self._setup_target(org)
        res = client.patch(
            f"/orgs/{org.id}/members/{target.id}/role/",
            {"role": RoleEnum.VIEWER},
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK
        assert res.data["role"] == RoleEnum.VIEWER

    @pytest.mark.parametrize("actor_role", [
        RoleEnum.MEMBER,
        RoleEnum.VIEWER,
        RoleEnum.BILLING,
    ])
    def test_lower_roles_denied_role_change(self, actor_role):
        org = OrganizationFactory()
        client = _make_client_for_role(actor_role, org)
        target = self._setup_target(org)
        res = client.patch(
            f"/orgs/{org.id}/members/{target.id}/role/",
            {"role": RoleEnum.VIEWER},
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_cannot_set_role_to_owner(self):
        """Ownership transfer must be blocked at the serializer layer."""
        org = OrganizationFactory()
        client = _make_client_for_role(RoleEnum.OWNER, org)
        target = self._setup_target(org)
        res = client.patch(
            f"/orgs/{org.id}/members/{target.id}/role/",
            {"role": RoleEnum.OWNER},
            format="json",
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST

    def test_cannot_change_own_role(self):
        """Self-modification must be rejected (400)."""
        org = OrganizationFactory()
        client = _make_client_for_role(RoleEnum.ADMIN, org)
        res = client.patch(
            f"/orgs/{org.id}/members/{client._user.id}/role/",
            {"role": RoleEnum.VIEWER},
            format="json",
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestRemoveMemberMatrix:
    """Column D — DELETE /orgs/<id>/members/<uid>/"""

    def _setup_target(self, org, target_role=RoleEnum.MEMBER):
        target = UserFactory(password="TestPass123!")
        MembershipFactory(organization=org, user=target, role=target_role)
        return target

    @pytest.mark.parametrize("actor_role", [RoleEnum.OWNER, RoleEnum.ADMIN])
    def test_admin_and_owner_can_remove_member(self, actor_role):
        org = OrganizationFactory()
        client = _make_client_for_role(actor_role, org)
        target = self._setup_target(org, RoleEnum.MEMBER)
        res = client.delete(f"/orgs/{org.id}/members/{target.id}/")
        assert res.status_code == status.HTTP_204_NO_CONTENT

    @pytest.mark.parametrize("actor_role", [
        RoleEnum.MEMBER,
        RoleEnum.VIEWER,
        RoleEnum.BILLING,
    ])
    def test_lower_roles_denied_removal(self, actor_role):
        org = OrganizationFactory()
        client = _make_client_for_role(actor_role, org)
        target = self._setup_target(org, RoleEnum.MEMBER)
        res = client.delete(f"/orgs/{org.id}/members/{target.id}/")
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_cannot_remove_owner(self):
        """The owner must be protected from removal."""
        org = OrganizationFactory()
        client = _make_client_for_role(RoleEnum.OWNER, org)
        # Create a second owner attempt — set a MEMBER, then try owner
        target = self._setup_target(org, RoleEnum.MEMBER)
        # Manually upgrade target to OWNER for this guard test
        from apps.tenants.models import OrganizationMembership
        OrganizationMembership.objects.filter(
            organization=org, user=target
        ).update(role=RoleEnum.OWNER)
        res = client.delete(f"/orgs/{org.id}/members/{target.id}/")
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_cannot_remove_self(self):
        """Self-removal must be rejected (400)."""
        org = OrganizationFactory()
        client = _make_client_for_role(RoleEnum.ADMIN, org)
        res = client.delete(f"/orgs/{org.id}/members/{client._user.id}/")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
