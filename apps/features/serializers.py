from rest_framework import serializers

from .models import FeatureFlag, ResourceSnapshot


class FeatureFlagSerializer(serializers.ModelSerializer):
    class Meta:
        model = FeatureFlag
        fields = [
            "id",
            "key",
            "description",
            "enabled_default",
            "enabled_for_plans",
            "enabled_for_orgs",
            "rollout_pct",
            "metadata",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class FeatureFlagEvaluationSerializer(serializers.Serializer):
    """
    Serializer for the `/me/features` endpoint.
    Returns a flat map of feature_key -> enabled (bool).
    """

    def to_representation(self, instance):
        """
        instance is a dict {feature_key: bool} or an object with .features property
        """
        if isinstance(instance, dict):
            return instance
        return getattr(instance, "features", {})


class ResourceSnapshotSerializer(serializers.ModelSerializer):
    """
    Serializer for ResourceSnapshot with polymorphic ID field support.
    
    Handles UUID and integer ID types transparently.
    """

    # Convert polymorphic IDs to strings for JSON serialization
    resource_id = serializers.SerializerMethodField()
    organization_id = serializers.SerializerMethodField()
    actor_id = serializers.SerializerMethodField()

    class Meta:
        model = ResourceSnapshot
        fields = [
            "id",
            "resource_type",
            "resource_id",
            "organization_id",
            "version",
            "data",
            "actor_id",
            "request_id",
            "change_reason",
            "snapshot_metadata",
            "created_at",
        ]
        read_only_fields = fields

    def get_resource_id(self, obj):
        """Serialize polymorphic resource_id to string."""
        return str(obj.resource_id) if obj.resource_id else None

    def get_organization_id(self, obj):
        """Serialize polymorphic organization_id to string."""
        return str(obj.organization_id) if obj.organization_id else None

    def get_actor_id(self, obj):
        """Serialize polymorphic actor_id to string."""
        return str(obj.actor_id) if obj.actor_id else None


class ResourceSnapshotDetailSerializer(ResourceSnapshotSerializer):
    """
    Extended serializer for detailed snapshot view with computed fields.
    """

    # Computed fields
    resource_display = serializers.SerializerMethodField()
    snapshot_age_seconds = serializers.SerializerMethodField()
    is_current_version = serializers.SerializerMethodField()

    class Meta:
        model = ResourceSnapshot
        fields = ResourceSnapshotSerializer.Meta.fields + [
            "resource_display",
            "snapshot_age_seconds",
            "is_current_version",
        ]
        read_only_fields = fields

    def get_resource_display(self, obj):
        """Display resource identifier."""
        return f"{obj.resource_type}#{obj.resource_id}"

    def get_snapshot_age_seconds(self, obj):
        """Get snapshot age in seconds."""
        from django.utils import timezone

        delta = timezone.now() - obj.created_at
        return delta.total_seconds()

    def get_is_current_version(self, obj):
        """Check if this is the current version of the resource."""
        # Find the latest version for this resource
        latest = (
            ResourceSnapshot.objects.filter(
                resource_type=obj.resource_type,
                resource_id=obj.resource_id,
            )
            .order_by("-version")
            .first()
        )
        return latest and latest.version == obj.version


class SnapshotComparisonSerializer(serializers.Serializer):
    """
    Serializer for snapshot comparison results.
    """

    from_version = serializers.IntegerField()
    to_version = serializers.IntegerField()
    diff = serializers.JSONField()
    from_snapshot_id = serializers.IntegerField()
    to_snapshot_id = serializers.IntegerField()


class SnapshotRestoreSerializer(serializers.Serializer):
    """
    Serializer for snapshot restoration request and response.
    """

    snapshot_id = serializers.IntegerField()
    apply_changes = serializers.BooleanField(default=True)
    status = serializers.CharField(read_only=True)
    message = serializers.CharField(read_only=True)
    task_id = serializers.CharField(read_only=True, allow_null=True)
    updated_fields = serializers.ListField(child=serializers.CharField(), read_only=True)
