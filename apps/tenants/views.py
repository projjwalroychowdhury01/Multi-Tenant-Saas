"""
Tenant member-management views.

All endpoints live under /orgs/<org_id>/members/ (mounted at /orgs/ in root urls.py).

GET    /orgs/<org_id>/members/           — list all members in the org
PATCH  /orgs/<org_id>/members/<uid>/     — change a member's role
DELETE /orgs/<org_id>/members/<uid>/     — remove a member from the org

Security contract
─────────────────
  1. The requesting user must be authenticated (JWT required).
  2. The requesting user must be a member of the org identified by <org_id>.
     If they are not a member, we return 404 (not 403) to avoid confirming
     the existence of that org to a foreign tenant.
  3. Role-change and delete operations require the ADMIN or OWNER role
     (``users:manage`` permission).
  4. An ADMIN cannot remove or demote the OWNER.
  5. A user cannot remove themselves via this endpoint.
"""

import logging
import uuid

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.rbac.permissions import _get_request_role, CanReadUsers, CanManageUsers
from apps.rbac.registry import is_at_least, role_rank
from apps.tenants.models import Organization, OrganizationMembership, RoleEnum
from apps.tenants.serializers import (
    ChangeRoleSerializer,
    OrganizationMembershipSerializer,
)

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _resolve_org_or_404(request, org_id: str) -> Organization:
    """
    Return the Organization if the requesting user is a member, else 404.

    This intentionally never returns 403 — the org's existence itself is
    confidential to non-members.
    """
    try:
        org = Organization.all_objects.get(id=org_id, is_active=True)
    except (Organization.DoesNotExist, ValueError):
        from rest_framework.exceptions import NotFound
        raise NotFound()

    # Verify the caller belongs to this org
    is_member = OrganizationMembership.objects.filter(
        organization=org, user=request.user
    ).exists()
    if not is_member:
        from rest_framework.exceptions import NotFound
        raise NotFound()

    return org


# ── List Members ──────────────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_members(request, org_id):
    """
    GET /orgs/<org_id>/members/

    Returns all members in the org with their roles.
    Requires: ``users:read`` permission (MEMBER, VIEWER, ADMIN, OWNER).
    """
    org = _resolve_org_or_404(request, org_id)

    # Permission check: need at least users:read
    role = _get_request_role(request)
    from apps.rbac.registry import has_permission
    if not has_permission(role, "users:read"):
        return Response(
            {
                "error": "You do not have permission to list members.",
                "code": "permission_denied",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    memberships = (
        OrganizationMembership.objects.filter(organization=org)
        .select_related("user")
        .order_by("joined_at")
    )
    serializer = OrganizationMembershipSerializer(memberships, many=True)
    return Response({"results": serializer.data, "count": memberships.count()})


# ── Change Member Role ────────────────────────────────────────────────────────


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def change_member_role(request, org_id, uid):
    """
    PATCH /orgs/<org_id>/members/<uid>/

    Change a member's role.  Requires ``users:manage`` permission (ADMIN+).

    Restrictions:
      - Cannot set role to OWNER (ownership transfer requires a separate flow).
      - A non-OWNER requester cannot act on the OWNER membership.
      - A user cannot change their own role.
    """
    org = _resolve_org_or_404(request, org_id)

    requester_role = _get_request_role(request)
    from apps.rbac.registry import has_permission
    if not has_permission(requester_role, "users:manage"):
        return Response(
            {
                "error": "Only Admins and Owners can change member roles.",
                "code": "permission_denied",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    # Resolve target membership — 404 if not in this org
    try:
        target_uid = uuid.UUID(str(uid))
    except (ValueError, AttributeError):
        return Response(
            {"error": "Invalid user ID.", "code": "invalid_id"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    membership = get_object_or_404(
        OrganizationMembership,
        organization=org,
        user__id=target_uid,
    )

    # Guard: no self-role-change
    if membership.user == request.user:
        return Response(
            {"error": "You cannot change your own role.", "code": "self_modification"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Guard: non-OWNER cannot touch the OWNER's membership
    if membership.role == RoleEnum.OWNER and requester_role != RoleEnum.OWNER:
        return Response(
            {
                "error": "Only the Owner can modify another Owner's membership.",
                "code": "owner_protected",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    # Guard: ADMIN cannot elevate someone above their own rank
    if (
        requester_role == RoleEnum.ADMIN
        and role_rank(membership.role) >= role_rank(RoleEnum.ADMIN)
    ):
        return Response(
            {
                "error": "Admins cannot modify the role of another Admin or Owner.",
                "code": "rank_violation",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    serializer = ChangeRoleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    old_role = membership.role
    membership.role = serializer.validated_data["role"]
    membership.save(update_fields=["role"])

    logger.info(
        "Role changed: user=%s org=%s %s→%s by=%s",
        membership.user_id,
        org.id,
        old_role,
        membership.role,
        request.user.id,
    )

    return Response(OrganizationMembershipSerializer(membership).data)


# ── Remove Member ─────────────────────────────────────────────────────────────


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def remove_member(request, org_id, uid):
    """
    DELETE /orgs/<org_id>/members/<uid>/

    Remove a member from the org.  Requires ``users:manage`` (ADMIN+).

    Restrictions:
      - The OWNER cannot be removed.
      - A user cannot remove themselves.
    """
    org = _resolve_org_or_404(request, org_id)

    requester_role = _get_request_role(request)
    from apps.rbac.registry import has_permission
    if not has_permission(requester_role, "users:manage"):
        return Response(
            {
                "error": "Only Admins and Owners can remove members.",
                "code": "permission_denied",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        target_uid = uuid.UUID(str(uid))
    except (ValueError, AttributeError):
        return Response(
            {"error": "Invalid user ID.", "code": "invalid_id"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    membership = get_object_or_404(
        OrganizationMembership,
        organization=org,
        user__id=target_uid,
    )

    # Guard: cannot remove self
    if membership.user == request.user:
        return Response(
            {
                "error": "You cannot remove yourself from the organisation.",
                "code": "self_removal",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Guard: OWNER cannot be removed — must transfer ownership first
    if membership.role == RoleEnum.OWNER:
        return Response(
            {
                "error": "The Owner cannot be removed. Transfer ownership first.",
                "code": "owner_protected",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    # Guard: ADMIN cannot remove another ADMIN (only OWNER can)
    if membership.role == RoleEnum.ADMIN and requester_role != RoleEnum.OWNER:
        return Response(
            {
                "error": "Only the Owner can remove another Admin.",
                "code": "rank_violation",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    user_id = membership.user_id
    membership.delete()

    logger.info(
        "Member removed: user=%s org=%s by=%s",
        user_id,
        org.id,
        request.user.id,
    )

    return Response(status=status.HTTP_204_NO_CONTENT)
