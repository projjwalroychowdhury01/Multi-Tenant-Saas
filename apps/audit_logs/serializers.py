"""
Serializers for the AuditLog API.
"""

from rest_framework import serializers

from apps.audit_logs.models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.SerializerMethodField()
    org_slug = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "actor",
            "actor_email",
            "org",
            "org_slug",
            "action",
            "resource_type",
            "resource_id",
            "diff",
            "ip_address",
            "user_agent",
            "request_id",
            "created_at",
        ]
        read_only_fields = fields

    def get_actor_email(self, obj):
        return obj.actor.email if obj.actor else None

    def get_org_slug(self, obj):
        return obj.org.slug if obj.org else None
