"""
RBAC Permission Registry.

Maps each RoleEnum value to the exact set of permission namespaces that role
holds. Every scope follows the pattern  ``<resource>:<action>``.

Permission scopes
─────────────────
  users:read          — read any user profile within the org
  users:invite        — send invitations to new users
  users:manage        — change roles, remove members
  api_keys:read       — list API keys (names/prefixes only, never secrets)
  api_keys:manage     — create / revoke API keys
  settings:read       — view org settings
  settings:manage     — update org name, plan metadata, etc.
  billing:read        — view invoices, subscription status
  billing:manage      — update payment method, cancel plan
  analytics:read      — view dashboards and reports

Role → permission mapping (highest authority first)
────────────────────────────────────────────────────
  OWNER   — all permissions (supersets every other role)
  ADMIN   — all except billing:manage and org destruction
  MEMBER  — read resources + manage own API keys
  VIEWER  — read-only across every resource category
  BILLING — billing read+write only (no product access)

Usage
─────
  from apps.rbac.registry import get_role_permissions

  permissions = get_role_permissions(RoleEnum.ADMIN)
  if "users:invite" in permissions:
      ...
"""

from apps.tenants.models import RoleEnum

# ── Permission Scope Constants ─────────────────────────────────────────────────

# User management
USERS_READ = "users:read"
USERS_INVITE = "users:invite"
USERS_MANAGE = "users:manage"

# API key management
API_KEYS_READ = "api_keys:read"
API_KEYS_MANAGE = "api_keys:manage"

# Organisation settings
SETTINGS_READ = "settings:read"
SETTINGS_MANAGE = "settings:manage"

# Billing
BILLING_READ = "billing:read"
BILLING_MANAGE = "billing:manage"

# Analytics / reporting
ANALYTICS_READ = "analytics:read"

# ── All permissions in one set (convenient for OWNER) ─────────────────────────

_ALL_PERMISSIONS: frozenset[str] = frozenset(
    [
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
    ]
)

# ── Role → Permission Mapping ──────────────────────────────────────────────────

PERMISSION_REGISTRY: dict[str, frozenset[str]] = {
    RoleEnum.OWNER: _ALL_PERMISSIONS,
    RoleEnum.ADMIN: frozenset(
        [
            USERS_READ,
            USERS_INVITE,
            USERS_MANAGE,
            API_KEYS_READ,
            API_KEYS_MANAGE,
            SETTINGS_READ,
            SETTINGS_MANAGE,
            BILLING_READ,     # ADMINs can see billing but cannot change it
            ANALYTICS_READ,
        ]
    ),
    RoleEnum.MEMBER: frozenset(
        [
            USERS_READ,
            API_KEYS_READ,
            API_KEYS_MANAGE,  # own keys only — enforced at the view layer
            SETTINGS_READ,
            ANALYTICS_READ,
        ]
    ),
    RoleEnum.VIEWER: frozenset(
        [
            USERS_READ,
            API_KEYS_READ,
            SETTINGS_READ,
            BILLING_READ,
            ANALYTICS_READ,
        ]
    ),
    RoleEnum.BILLING: frozenset(
        [
            BILLING_READ,
            BILLING_MANAGE,
        ]
    ),
}


# ── Public API ─────────────────────────────────────────────────────────────────


def get_role_permissions(role: str) -> frozenset[str]:
    """
    Return the frozenset of permission strings for the given role.

    Args:
        role: One of the RoleEnum string values (e.g. ``"ADMIN"``).

    Returns:
        A frozenset of permission scope strings.  Returns an empty
        frozenset for unknown roles so callers never receive ``None``.
    """
    return PERMISSION_REGISTRY.get(role, frozenset())


def has_permission(role: str, scope: str) -> bool:
    """
    Convenience helper — True if *role* includes *scope*.

    Args:
        role:  RoleEnum string value.
        scope: Permission scope string (e.g. ``"users:manage"``).
    """
    return scope in get_role_permissions(role)


def role_rank(role: str) -> int:
    """
    Return an integer rank so roles can be compared.
    Higher value = more authority.

    OWNER=5, ADMIN=4, MEMBER=3, VIEWER=2, BILLING=1, unknown=0
    """
    _RANKS = {
        RoleEnum.OWNER: 5,
        RoleEnum.ADMIN: 4,
        RoleEnum.MEMBER: 3,
        RoleEnum.VIEWER: 2,
        RoleEnum.BILLING: 1,
    }
    return _RANKS.get(role, 0)


def is_at_least(role: str, minimum_role: str) -> bool:
    """Return True if *role* has authority equal to or greater than *minimum_role*."""
    return role_rank(role) >= role_rank(minimum_role)
