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
            "created_at",
        ]
        read_only_fields = fields
