"""
Serializers for Organization and Membership.

OrganizationSerializer         — public org representation
OrganizationMembershipSerializer — safe member detail (Phase 2+)
ChangeRoleSerializer           — validates role change requests
"""

from rest_framework import serializers

from apps.tenants.models import Organization, OrganizationMembership, RoleEnum
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

