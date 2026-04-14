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
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rbac.permissions import _get_request_role, CanReadUsers, CanManageUsers
from apps.rbac.registry import is_at_least, role_rank, has_permission
from apps.tenants.models import (
    Organization,
    OrganizationMembership,
    OrganizationInvitation,
    InvitationStatus,
    RoleEnum,
)
from apps.tenants.serializers import (
    ChangeRoleSerializer,
    OrganizationMembershipSerializer,
    OrganizationInvitationSerializer,
    PublicInvitationSerializer,
    CreateInvitationSerializer,
    AcceptInvitationSerializer,
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


# ── Invitation Views (Phase 3) ──────────────────────────────────────────────


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def list_or_create_invitations(request, org_id):
    """
    GET  /orgs/<org_id>/invitations/  — list all invitations (requires users:read)
    POST /orgs/<org_id>/invitations/  — create invitation   (requires users:invite)
    """
    org = _resolve_org_or_404(request, org_id)
    requester_role = _get_request_role(request)

    if request.method == "GET":
        if not has_permission(requester_role, "users:read"):
            return Response(
                {"error": "You do not have permission to view invitations.", "code": "permission_denied"},
                status=status.HTTP_403_FORBIDDEN,
            )

        invitations = (
            OrganizationInvitation.objects.filter(organization=org)
            .select_related("invited_by")
            .order_by("-created_at")
        )
        serializer = OrganizationInvitationSerializer(invitations, many=True)
        return Response({"results": serializer.data, "count": invitations.count()})

    # POST — create new invitation
    if not has_permission(requester_role, "users:invite"):
        return Response(
            {"error": "Only Admins and Owners can send invitations.", "code": "permission_denied"},
            status=status.HTTP_403_FORBIDDEN,
        )

    serializer = CreateInvitationSerializer(
        data=request.data,
        context={"organization": org},
    )
    serializer.is_valid(raise_exception=True)

    # ADMIN cannot invite someone to ADMIN rank or above
    new_role = serializer.validated_data["role"]
    if requester_role == RoleEnum.ADMIN and role_rank(new_role) >= role_rank(RoleEnum.ADMIN):
        return Response(
            {
                "error": "Admins cannot invite users to Admin rank or above.",
                "code": "rank_violation",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    invitation = OrganizationInvitation.objects.create(
        organization=org,
        email=serializer.validated_data["email"],
        role=new_role,
        invited_by=request.user,
    )

    logger.info(
        "Invitation created: inv=%s email=%s org=%s role=%s by=%s",
        invitation.id,
        invitation.email,
        org.id,
        invitation.role,
        request.user.id,
    )

    return Response(
        OrganizationInvitationSerializer(invitation).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def revoke_invitation(request, org_id, inv_id):
    """
    DELETE /orgs/<org_id>/invitations/<inv_id>/

    Revoke (expire) a PENDING invitation.  Requires ``users:invite`` (ADMIN+).
    Only PENDING invitations can be revoked; already-ACCEPTED ones cannot.
    """
    org = _resolve_org_or_404(request, org_id)
    requester_role = _get_request_role(request)

    if not has_permission(requester_role, "users:invite"):
        return Response(
            {"error": "Only Admins and Owners can revoke invitations.", "code": "permission_denied"},
            status=status.HTTP_403_FORBIDDEN,
        )

    invitation = get_object_or_404(
        OrganizationInvitation,
        id=inv_id,
        organization=org,
    )

    if invitation.status != InvitationStatus.PENDING:
        return Response(
            {
                "error": f"Cannot revoke an invitation that is already {invitation.status}.",
                "code": "invalid_state",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    invitation.status = InvitationStatus.EXPIRED
    invitation.save(update_fields=["status"])

    logger.info(
        "Invitation revoked: inv=%s email=%s org=%s by=%s",
        invitation.id,
        invitation.email,
        org.id,
        request.user.id,
    )

    return Response(status=status.HTTP_204_NO_CONTENT)


class PublicInvitationView(APIView):
    """
    Unauthenticated + authenticated endpoints keyed on token.

    GET  /invitations/<token>/        — resolve token metadata (public)
    POST /invitations/<token>/accept/ — accept invite (authenticated)
    """

    def get_permissions(self):
        """GET is open; POST requires authentication."""
        if self.request.method == "GET":
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request, token):
        """
        Return safe metadata about the invitation so the frontend can
        render an "Accept Invite" screen without the user logging in first.
        """
        invitation = get_object_or_404(
            OrganizationInvitation.objects.select_related("organization", "invited_by"),
            token=token,
        )

        if invitation.status != InvitationStatus.PENDING:
            return Response(
                {
                    "error": f"This invitation is no longer valid (status: {invitation.status}).",
                    "code": "invitation_not_pending",
                },
                status=status.HTTP_410_GONE,
            )

        return Response(PublicInvitationSerializer(invitation).data)

    def post(self, request, token):
        """
        Accept the invitation.

        Validates:
          - Authenticated user's email must match the invited email.
          - Invitation must still be PENDING.
          - User must not already be a member of the org.
        """
        invitation = get_object_or_404(
            OrganizationInvitation.objects.select_related("organization", "invited_by"),
            token=token,
        )

        if invitation.status != InvitationStatus.PENDING:
            return Response(
                {
                    "error": f"This invitation is no longer valid (status: {invitation.status}).",
                    "code": "invitation_not_pending",
                },
                status=status.HTTP_410_GONE,
            )

        # Security: authenticated user's email must match the invited email
        if request.user.email.lower() != invitation.email.lower():
            return Response(
                {
                    "error": "Your account email does not match the invitation email.",
                    "code": "email_mismatch",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Guard: already a member?
        already_member = OrganizationMembership.objects.filter(
            organization=invitation.organization,
            user=request.user,
        ).exists()
        if already_member:
            return Response(
                {
                    "error": "You are already a member of this organisation.",
                    "code": "already_member",
                },
                status=status.HTTP_409_CONFLICT,
            )

        membership = invitation.accept(request.user)

        logger.info(
            "Invitation accepted: inv=%s user=%s org=%s role=%s",
            invitation.id,
            request.user.id,
            invitation.organization_id,
            membership.role,
        )

        return Response(
            {
                "message": "You have successfully joined the organisation.",
                "org_id": str(invitation.organization_id),
                "org_name": invitation.organization.name,
                "role": membership.role,
            },
            status=status.HTTP_200_OK,
        )
