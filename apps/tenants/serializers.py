"""
Serializers for Organization and Membership.

OrganizationSerializer         — public org representation
OrganizationMembershipSerializer — safe member detail (Phase 2+)
ChangeRoleSerializer           — validates role change requests
"""

from rest_framework import serializers

from apps.tenants.models import Organization, OrganizationMembership, OrganizationInvitation, InvitationStatus, RoleEnum
from apps.rbac.registry import role_rank


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "plan", "is_active", "created_at"]
        read_only_fields = ["id", "slug", "created_at"]


class OrganizationMembershipSerializer(serializers.ModelSerializer):
    """
    Safe, read-only representation of an org member.

    Exposes:
      - user identifiers (id, email, display name)
      - their role in *this* organisation
      - when they joined

    Intentionally omits hashed passwords, JWT tokens, and other
    sensitive user attributes.
    """

    user_id = serializers.UUIDField(source="user.id", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.CharField(source="user.display_name", read_only=True)
    is_verified = serializers.BooleanField(source="user.is_verified", read_only=True)

    class Meta:
        model = OrganizationMembership
        fields = [
            "id",
            "user_id",
            "user_email",
            "user_name",
            "is_verified",
            "role",
            "joined_at",
        ]
        read_only_fields = [
            "id",
            "user_id",
            "user_email",
            "user_name",
            "is_verified",
            "joined_at",
        ]


# Kept for backward-compatibility with existing imports
MembershipSerializer = OrganizationMembershipSerializer


class ChangeRoleSerializer(serializers.Serializer):
    """
    Validates a request to change a member's role.

    Rules enforced here (view also enforces them):
      - The new role must be a valid RoleEnum choice.
      - Only one OWNER may exist — the OWNER cannot be set via this endpoint;
        ownership transfer is a dedicated, two-step operation (Phase 3+).

    The requester's role authority is checked in the view / permission class,
    not here — serializers should not have access to request context beyond
    what is passed in explicitly.
    """

    role = serializers.ChoiceField(choices=RoleEnum.choices)

    def validate_role(self, value):
        if value == RoleEnum.OWNER:
            raise serializers.ValidationError(
                "Ownership transfer is not permitted through this endpoint."
            )
        return value


# ── Invitation Serializers (Phase 3) ───────────────────────────────────────────


class OrganizationInvitationSerializer(serializers.ModelSerializer):
    """
    Read-only representation of an invitation for listing / detail views.
    Exposes the inviter's email so the frontend can display "Invited by X".
    """

    invited_by_email = serializers.EmailField(
        source="invited_by.email", read_only=True, default=None
    )
    invited_by_name = serializers.CharField(
        source="invited_by.display_name", read_only=True, default=None
    )
    org_name = serializers.CharField(source="organization.name", read_only=True)

    class Meta:
        model = OrganizationInvitation
        fields = [
            "id",
            "email",
            "role",
            "status",
            "token",
            "org_name",
            "invited_by_email",
            "invited_by_name",
            "created_at",
        ]
        read_only_fields = fields


class PublicInvitationSerializer(serializers.ModelSerializer):
    """
    Safe, unauthenticated-accessible representation of an invitation.
    Intentionally omits the token itself and internal IDs.
    Only enough info for the frontend "Accept Invite" screen.
    """

    org_name = serializers.CharField(source="organization.name", read_only=True)
    org_slug = serializers.CharField(source="organization.slug", read_only=True)
    invited_by_name = serializers.CharField(
        source="invited_by.display_name", read_only=True, default=None
    )

    class Meta:
        model = OrganizationInvitation
        fields = ["id", "email", "role", "status", "org_name", "org_slug", "invited_by_name", "created_at"]
        read_only_fields = fields


class CreateInvitationSerializer(serializers.Serializer):
    """
    Validates a request to create a new invitation.

    Business rules enforced:
      1. Role cannot be OWNER — ownership is created at org-founding time only.
      2. Email must not already belong to an active member of the org.
      3. A PENDING invitation for this email+org pair must not already exist.

    The `organization` context must be injected via `context={"organization": org}`
    before calling `.is_valid()`.
    """

    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=RoleEnum.choices, default=RoleEnum.MEMBER)

    def validate_role(self, value):
        if value == RoleEnum.OWNER:
            raise serializers.ValidationError(
                "Users cannot be invited directly to the OWNER role."
            )
        return value

    def validate(self, data):
        org = self.context.get("organization")
        email = data["email"]

        if org is None:
            raise serializers.ValidationError("Organization context is required.")

        # Rule 2: email must not already be a member
        already_member = OrganizationMembership.objects.filter(
            organization=org, user__email__iexact=email
        ).exists()
        if already_member:
            raise serializers.ValidationError(
                {"email": f"{email} is already a member of this organisation."}
            )

        # Rule 3: no duplicate PENDING invite
        already_invited = OrganizationInvitation.objects.filter(
            organization=org,
            email__iexact=email,
            status=InvitationStatus.PENDING,
        ).exists()
        if already_invited:
            raise serializers.ValidationError(
                {"email": f"A pending invitation for {email} already exists."}
            )

        return data


class AcceptInvitationSerializer(serializers.Serializer):
    """
    Empty body serializer for ``POST /invitations/<token>/accept/``.

    Exists purely for OpenAPI schema completeness; there is no request body.
    DRF-Spectacular will generate a 200 response schema from the view's
    explicit serializer annotation.
    """
    pass
