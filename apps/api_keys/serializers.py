"""
Serializers for the API key management endpoints.

Design
──────
  ApiKeyCreateSerializer   — used only on POST /api-keys.
                              Returns the plaintext secret ONCE in the response.
                              After that, it is gone forever.
  ApiKeyListSerializer     — used for GET /api-keys.
                              Masks the secret: shows only name, prefix,
                              last_4, scopes, env, expires_at, last_used_at.
  ApiKeyDetailSerializer   — for PATCH /api-keys/{id}.
                              Allows updating name, scopes, expires_at.
  ApiKeyRotateSerializer   — empty body for POST /api-keys/{id}/rotate.

Security invariant: `hashed_key` and the full secret NEVER appear in any
serializer output.
"""

from django.utils import timezone

from rest_framework import serializers

from apps.api_keys.models import ApiKey, EnvChoices


class ApiKeyCreateSerializer(serializers.ModelSerializer):
    """
    Validates and creates a new API key.

    The `secret` field is write_only=True and is populated by the view after
    the instance is saved so it can be returned in the response exactly once.
    """

    # These are set by the view after generation — not submitted by the client
    secret = serializers.CharField(read_only=True, help_text="Plaintext secret. Shown ONCE.")

    class Meta:
        model = ApiKey
        fields = ["id", "name", "env", "scopes", "expires_at", "secret"]
        read_only_fields = ["id", "secret"]

    def validate_env(self, value):
        if value not in [EnvChoices.LIVE, EnvChoices.TEST]:
            raise serializers.ValidationError(f"env must be one of: {EnvChoices.values}")
        return value

    def validate_scopes(self, value):
        """Reject unknown scope strings to prevent silent misconfiguration."""
        from apps.rbac.registry import _ALL_PERMISSIONS

        invalid = [s for s in value if s not in _ALL_PERMISSIONS]
        if invalid:
            raise serializers.ValidationError(
                f"Unknown permission scopes: {invalid}. "
                f"Valid scopes: {sorted(_ALL_PERMISSIONS)}"
            )
        return value


class ApiKeyListSerializer(serializers.ModelSerializer):
    """
    Safe read-only serializer — NEVER exposes the full secret or hash.

    last_4 is derived from the prefix for display only (last 4 chars of prefix).
    """

    last_4 = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()

    class Meta:
        model = ApiKey
        fields = [
            "id",
            "name",
            "prefix",
            "last_4",
            "env",
            "scopes",
            "is_active",
            "is_expired",
            "expires_at",
            "last_used_at",
            "created_at",
        ]

    def get_last_4(self, obj) -> str:
        """Return the last 4 chars of the prefix as a visual hint."""
        return obj.prefix[-4:] if len(obj.prefix) >= 4 else obj.prefix

    def get_is_expired(self, obj) -> bool:
        if obj.expires_at is None:
            return False
        return obj.expires_at < timezone.now()


class ApiKeyUpdateSerializer(serializers.ModelSerializer):
    """Allows updating mutable fields on an existing key."""

    class Meta:
        model = ApiKey
        fields = ["name", "scopes", "expires_at", "is_active"]

    def validate_scopes(self, value):
        from apps.rbac.registry import _ALL_PERMISSIONS

        invalid = [s for s in value if s not in _ALL_PERMISSIONS]
        if invalid:
            raise serializers.ValidationError(f"Unknown permission scopes: {invalid}.")
        return value
