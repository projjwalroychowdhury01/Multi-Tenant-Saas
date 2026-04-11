"""Serializers for Organization and Membership."""

from rest_framework import serializers

from apps.tenants.models import Organization, OrganizationMembership


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "plan", "is_active", "created_at"]
        read_only_fields = ["id", "slug", "created_at"]


class MembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = OrganizationMembership
        fields = ["id", "user_id", "user_email", "user_name", "role", "joined_at"]
        read_only_fields = ["id", "user_id", "user_email", "user_name", "joined_at"]
