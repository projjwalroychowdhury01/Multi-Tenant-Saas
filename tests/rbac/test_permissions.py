"""
Unit tests for apps/rbac/registry.py and apps/rbac/permissions.py.

Tests validate:
  - PERMISSION_REGISTRY correctness per role
  - get_role_permissions() / has_permission() helpers
  - role_rank() and is_at_least() ordering
  - HasTenantPermission DRF class logic (mocked request)
  - require_permission decorator logic (mocked request)
"""

from unittest.mock import MagicMock

import pytest
from rest_framework.exceptions import PermissionDenied

from apps.rbac.permissions import HasTenantPermission, require_permission
from apps.rbac.registry import (
    ANALYTICS_READ,
    API_KEYS_MANAGE,
    API_KEYS_READ,
    BILLING_MANAGE,
    BILLING_READ,
    PERMISSION_REGISTRY,
    SETTINGS_MANAGE,
    SETTINGS_READ,
    USERS_INVITE,
    USERS_MANAGE,
    USERS_READ,
    get_role_permissions,
    has_permission,
    is_at_least,
    role_rank,
)
from apps.tenants.models import RoleEnum

# ── Registry unit tests ────────────────────────────────────────────────────────


class TestPermissionRegistry:
    """Validate the PERMISSION_REGISTRY mapping is correct and complete."""

    def test_all_roles_in_registry(self):
        """Every RoleEnum value must have an entry in the registry."""
        for role in RoleEnum.values:
            assert role in PERMISSION_REGISTRY, f"Missing registry entry for {role}"

    def test_owner_has_all_permissions(self):
        """OWNER must hold every defined permission."""
        all_scopes = {
            USERS_READ,
            USERS_INVITE,
            USERS_MANAGE,
            API_KEYS_READ,
            API_KEYS_MANAGE,
            SETTINGS_READ,
            SETTINGS_MANAGE,
            BILLING_READ,
            BILLING_MANAGE,
            ANALYTICS_READ,
        }
        owner_perms = get_role_permissions(RoleEnum.OWNER)
        assert all_scopes <= owner_perms, f"OWNER missing: {all_scopes - owner_perms}"

    def test_admin_cannot_manage_billing(self):
        assert not has_permission(RoleEnum.ADMIN, BILLING_MANAGE)

    def test_admin_can_read_billing(self):
        assert has_permission(RoleEnum.ADMIN, BILLING_READ)

    def test_admin_can_invite_users(self):
        assert has_permission(RoleEnum.ADMIN, USERS_INVITE)

    def test_admin_can_manage_users(self):
        assert has_permission(RoleEnum.ADMIN, USERS_MANAGE)

    def test_member_can_manage_own_api_keys(self):
        assert has_permission(RoleEnum.MEMBER, API_KEYS_MANAGE)

    def test_member_cannot_manage_users(self):
        assert not has_permission(RoleEnum.MEMBER, USERS_MANAGE)

    def test_member_cannot_invite(self):
        assert not has_permission(RoleEnum.MEMBER, USERS_INVITE)

    def test_viewer_is_strictly_read_only(self):
        """VIEWER must not hold any 'manage' or 'invite' scope."""
        viewer_perms = get_role_permissions(RoleEnum.VIEWER)
        for scope in viewer_perms:
            assert "manage" not in scope, f"VIEWER should not have {scope}"
            assert "invite" not in scope, f"VIEWER should not have {scope}"

    def test_billing_has_only_billing_scopes(self):
        billing_perms = get_role_permissions(RoleEnum.BILLING)
        assert billing_perms == frozenset([BILLING_READ, BILLING_MANAGE])

    def test_unknown_role_returns_empty(self):
        result = get_role_permissions("NONEXISTENT_ROLE")
        assert result == frozenset()

    def test_permissions_are_frozensets(self):
        """Registry values must be frozensets (immutable, hashable)."""
        for role, perms in PERMISSION_REGISTRY.items():
            assert isinstance(perms, frozenset), f"Role {role} perms is not frozenset"


class TestRoleRank:
    """Validate role_rank() and is_at_least() ordering."""

    def test_rank_ordering(self):
        assert role_rank(RoleEnum.OWNER) > role_rank(RoleEnum.ADMIN)
        assert role_rank(RoleEnum.ADMIN) > role_rank(RoleEnum.MEMBER)
        assert role_rank(RoleEnum.MEMBER) > role_rank(RoleEnum.VIEWER)
        assert role_rank(RoleEnum.VIEWER) > role_rank(RoleEnum.BILLING)

    def test_unknown_role_rank_is_zero(self):
        assert role_rank("GHOST") == 0

    def test_is_at_least_reflexive(self):
        for role in RoleEnum.values:
            assert is_at_least(role, role), f"is_at_least({role}, {role}) should be True"

    def test_owner_is_at_least_all(self):
        for role in RoleEnum.values:
            assert is_at_least(RoleEnum.OWNER, role)

    def test_billing_is_not_at_least_viewer(self):
        assert not is_at_least(RoleEnum.BILLING, RoleEnum.VIEWER)

    def test_viewer_is_not_at_least_member(self):
        assert not is_at_least(RoleEnum.VIEWER, RoleEnum.MEMBER)


# ── HasTenantPermission tests (mocked request) ────────────────────────────────


def _make_request(role: str | None, is_authenticated: bool = True, has_org: bool = True):
    """Build a minimal mock request for DRF permission testing."""
    request = MagicMock()
    request.user.is_authenticated = is_authenticated

    if has_org:
        request.org = MagicMock()
        request.org.id = "org-123"
    else:
        request.org = None

    # Token payload — drives _get_request_role
    if role:
        request.auth = MagicMock()
        request.auth.payload = {"role": role}
    else:
        request.auth = None

    return request


class TestHasTenantPermission:
    """Test the DRF permission class with mocked requests."""

    def _make_view(self, scope: str | None):
        view = MagicMock()
        view.required_scope = scope
        view.__class__.__name__ = "TestView"
        return view

    def test_owner_has_any_scope(self):
        perm = HasTenantPermission()
        request = _make_request(RoleEnum.OWNER)
        view = self._make_view("users:manage")
        assert perm.has_permission(request, view) is True

    def test_viewer_denied_manage_scope(self):
        perm = HasTenantPermission()
        request = _make_request(RoleEnum.VIEWER)
        view = self._make_view("users:manage")
        assert perm.has_permission(request, view) is False

    def test_viewer_allowed_read_scope(self):
        perm = HasTenantPermission()
        request = _make_request(RoleEnum.VIEWER)
        view = self._make_view("users:read")
        assert perm.has_permission(request, view) is True

    def test_billing_allowed_billing_manage(self):
        perm = HasTenantPermission()
        request = _make_request(RoleEnum.BILLING)
        view = self._make_view("billing:manage")
        assert perm.has_permission(request, view) is True

    def test_billing_denied_users_read(self):
        perm = HasTenantPermission()
        request = _make_request(RoleEnum.BILLING)
        view = self._make_view("users:read")
        assert perm.has_permission(request, view) is False

    def test_unauthenticated_denied(self):
        perm = HasTenantPermission()
        request = _make_request(None, is_authenticated=False)
        view = self._make_view("users:read")
        assert perm.has_permission(request, view) is False

    def test_no_org_denied(self):
        perm = HasTenantPermission()
        request = _make_request(RoleEnum.ADMIN, has_org=False)
        view = self._make_view("users:read")
        assert perm.has_permission(request, view) is False

    def test_no_scope_on_view_denied(self):
        """Misconfigured view (missing required_scope) must be denied."""
        perm = HasTenantPermission()
        request = _make_request(RoleEnum.OWNER)
        view = self._make_view(None)
        assert perm.has_permission(request, view) is False


# ── require_permission decorator tests ────────────────────────────────────────


class TestRequirePermissionDecorator:
    """Test the @require_permission function decorator."""

    def _make_fn_view(self, scope: str):
        """Returns a decorated dummy view function."""

        @require_permission(scope)
        def dummy_view(request):
            return "ok"

        return dummy_view

    def test_owner_passes_all_scopes(self):
        request = _make_request(RoleEnum.OWNER)
        view = self._make_fn_view("analytics:read")
        result = view(request)
        assert result == "ok"

    def test_viewer_denied_manage_scope(self):
        request = _make_request(RoleEnum.VIEWER)
        view = self._make_fn_view("users:manage")
        with pytest.raises(PermissionDenied):
            view(request)

    def test_billing_denied_settings_read(self):
        request = _make_request(RoleEnum.BILLING)
        view = self._make_fn_view("settings:read")
        with pytest.raises(PermissionDenied):
            view(request)

    def test_unauthenticated_raises(self):
        request = _make_request(None, is_authenticated=False)
        view = self._make_fn_view("users:read")
        with pytest.raises(PermissionDenied):
            view(request)

    def test_no_org_raises(self):
        request = _make_request(RoleEnum.ADMIN, has_org=False)
        view = self._make_fn_view("users:read")
        with pytest.raises(PermissionDenied):
            view(request)
