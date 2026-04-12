"""
DRF permission classes and view decorators for tenant-scoped RBAC.

Classes
───────
  HasTenantPermission   — base DRF permission; subclasses declare required_scope.
  require_permission    — function-based view decorator (wraps @api_view views).

Gotchas
───────
  * The JWT token carries `role` and `org_id` claims set by
    CustomTokenObtainPairSerializer. The auth layer resolves ``request.org``
    from those claims via TenantContextMiddleware.
  * If request.org is None (unauthenticated or no membership) every
    permission check returns False — no crash, just 403.
  * Object-level checks compare the request org against the target resource's
    org FK to prevent cross-tenant access.
"""

import functools
import logging

from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.rbac.registry import get_role_permissions, is_at_least
from apps.tenants.models import OrganizationMembership, RoleEnum

logger = logging.getLogger(__name__)


# ── Helper: resolve role for the current request ──────────────────────────────


def _get_request_role(request: Request) -> str | None:
    """
    Return the role string from the JWT payload for the current request.

    The role is injected into the JWT by CustomTokenObtainPairSerializer and
    decoded by simplejwt before the view is called.  We read it from the token
    payload directly to avoid an extra DB query.

    Falls back to a DB lookup if the token payload is missing the claim.
    """
    # Try the JWT payload first (zero DB queries)
    token_payload = getattr(request.auth, "payload", {}) if request.auth else {}
    role = token_payload.get("role")
    if role:
        return role

    # Fallback: look up from DB (e.g., for tests using force_authenticate)
    org = getattr(request, "org", None)
    user = getattr(request, "user", None)
    if org is None or user is None or not user.is_authenticated:
        return None

    try:
        membership = OrganizationMembership.objects.get(organization=org, user=user)
        return membership.role
    except OrganizationMembership.DoesNotExist:
        return None


# ── Base Permission Class ──────────────────────────────────────────────────────


class HasTenantPermission(permissions.BasePermission):
    """
    DRF permission class that gates views by tenant role and permission scope.

    Usage (class-based view)::

        class MyViewSet(ViewSet):
            permission_classes = [HasTenantPermission]
            required_scope = "users:manage"   # <── declare scope on the view

    The view *must* declare ``required_scope`` as a class attribute.
    If it is missing we deny access and log a configuration warning.
    """

    # Subclasses or view classes must set this
    required_scope: str | None = None

    message = "You do not have permission to perform this action."

    def _get_scope(self, view) -> str | None:
        """
        Resolve the required permission scope from the view.

        Checks (in order):
          1. ``view.required_scope`` (class attribute)
          2. ``self.required_scope`` (set on the permission instance, rare)
        """
        scope = getattr(view, "required_scope", None) or self.required_scope
        if scope is None:
            logger.warning(
                "HasTenantPermission applied to %s but required_scope is not set. "
                "Denying access by default.",
                view.__class__.__name__,
            )
        return scope

    def has_permission(self, request: Request, view) -> bool:
        user = request.user
        if not user or not user.is_authenticated:
            return False

        org = getattr(request, "org", None)
        if org is None:
            return False

        scope = self._get_scope(view)
        if scope is None:
            return False  # misconfigured view → deny

        role = _get_request_role(request)
        if role is None:
            return False

        granted = scope in get_role_permissions(role)
        if not granted:
            logger.debug(
                "Permission denied: user=%s org=%s role=%s required_scope=%s",
                user.id,
                org.id,
                role,
                scope,
            )
        return granted

    def has_object_permission(self, request: Request, view, obj) -> bool:
        """
        Object-level check: the object must belong to the request's org.

        Supports both TenantModel instances (with ``.organization`` FK) and
        OrganizationMembership instances (with ``.organization`` FK).
        """
        if not self.has_permission(request, view):
            return False

        org = getattr(request, "org", None)
        obj_org = getattr(obj, "organization", None)
        if org is None or obj_org is None:
            return False

        # Primary key comparison — avoids another query if obj_org is loaded
        return (
            obj_org.pk == org.pk
            if not isinstance(obj_org, type(org))
            else obj_org == org
        )


# ── Specialised permission subclasses ─────────────────────────────────────────


class CanReadUsers(HasTenantPermission):
    """Allows access to any role that has ``users:read``."""
    required_scope = "users:read"


class CanManageUsers(HasTenantPermission):
    """Restricted to ADMIN+ roles (``users:manage``)."""
    required_scope = "users:manage"


class CanInviteUsers(HasTenantPermission):
    """Restricted to ADMIN+ roles (``users:invite``)."""
    required_scope = "users:invite"


class CanReadBilling(HasTenantPermission):
    """VIEWER, BILLING, ADMIN, OWNER."""
    required_scope = "billing:read"


class CanManageBilling(HasTenantPermission):
    """BILLING and OWNER only (``billing:manage``)."""
    required_scope = "billing:manage"


class IsAtLeastAdmin(permissions.BasePermission):
    """
    Shortcut: only OWNER or ADMIN may proceed.

    Does NOT check a fine-grained scope — use this when you need a
    simple hierarchy check rather than a specific permission namespace.
    """

    message = "Only Admins and Owners can perform this action."

    def has_permission(self, request: Request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        role = _get_request_role(request)
        return role is not None and is_at_least(role, RoleEnum.ADMIN)


class IsOwner(permissions.BasePermission):
    """Only the OWNER of the org may proceed."""

    message = "Only the Owner can perform this action."

    def has_permission(self, request: Request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        role = _get_request_role(request)
        return role == RoleEnum.OWNER


# ── Function-based view decorator ─────────────────────────────────────────────


def require_permission(scope: str):
    """
    Decorator for ``@api_view`` function-based views.

    Raises ``PermissionDenied`` (HTTP 403) if the current user's role
    does not include *scope*.

    Usage::

        @api_view(["GET"])
        @require_permission("analytics:read")
        def my_view(request):
            ...

    Note: apply *after* ``@api_view`` (i.e., closer to the function).
    """

    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapped_view(request, *args, **kwargs):
            user = request.user
            if not user or not user.is_authenticated:
                raise PermissionDenied("Authentication required.")

            org = getattr(request, "org", None)
            if org is None:
                raise PermissionDenied("No active organisation context.")

            role = _get_request_role(request)
            if role is None or scope not in get_role_permissions(role):
                logger.debug(
                    "require_permission denied: user=%s role=%s scope=%s",
                    user.id,
                    role,
                    scope,
                )
                raise PermissionDenied(
                    f"Your role does not grant the '{scope}' permission."
                )
            return view_func(request, *args, **kwargs)

        return wrapped_view

    return decorator
