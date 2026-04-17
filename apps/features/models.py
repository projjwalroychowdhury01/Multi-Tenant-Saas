import json
import uuid
from typing import Union

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import CharField, JSONField
from django.utils import timezone


class PolymorphicIDField(models.Field):
    """
    Custom field that stores either UUID or BigInteger transparently.
    
    Serializes to JSON as {"type": "uuid" | "int", "value": <value>}
    Can be indexed and filtered like a normal field.
    
    Usage:
        resource_id = PolymorphicIDField(help_text="UUID or integer ID")
        
        # Assign either type:
        obj.resource_id = uuid.uuid4()
        obj.resource_id = 12345
        
        # Query transparently:
        ResourceSnapshot.objects.filter(resource_id="some-uuid-string")
        ResourceSnapshot.objects.filter(resource_id=123)
    """

    description = "Polymorphic field that stores UUID or BigInteger"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 255)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("max_length", None)
        return name, path, args, kwargs

    def db_type(self, connection):
        """Use VARCHAR for storage."""
        return "VARCHAR(255)"

    def get_prep_value(self, value):
        """Convert Python value to database-ready value."""
        if value is None:
            return None
        return self._serialize_value(value)

    def from_db_value(self, value, expression, connection):
        """Convert database value back to Python."""
        if value is None:
            return None
        return self._deserialize_value(value)

    @staticmethod
    def _serialize_value(value: Union[uuid.UUID, int, str]) -> str:
        """Serialize UUID or int to JSON string."""
        if isinstance(value, uuid.UUID):
            return json.dumps({"type": "uuid", "value": str(value)})
        elif isinstance(value, int):
            return json.dumps({"type": "int", "value": value})
        elif isinstance(value, str):
            # Try to parse as UUID first
            try:
                uuid_obj = uuid.UUID(value)
                return json.dumps({"type": "uuid", "value": str(uuid_obj)})
            except ValueError:
                # Try as int
                try:
                    int_val = int(value)
                    return json.dumps({"type": "int", "value": int_val})
                except ValueError:
                    # Assume it's a raw UUID string
                    return json.dumps({"type": "uuid", "value": value})
        return value

    @staticmethod
    def _deserialize_value(value: str) -> Union[uuid.UUID, int]:
        """Deserialize JSON string back to UUID or int."""
        if not isinstance(value, str):
            return value
        
        try:
            data = json.loads(value)
            if data.get("type") == "uuid":
                return uuid.UUID(data["value"])
            elif data.get("type") == "int":
                return data["value"]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        
        # Fallback: try UUID, then int
        try:
            return uuid.UUID(value)
        except ValueError:
            try:
                return int(value)
            except ValueError:
                return value

    def value_to_string(self, obj):
        """For serialization."""
        value = getattr(obj, self.attname)
        return self.get_prep_value(value)


class FeatureFlag(models.Model):
    """
    Feature flag model with plan-based defaults, per-org overrides, and rollout percentage support.
    
    Evaluation logic:
    1. Check if explicitly enabled for this org in `enabled_for_orgs`
    2. Check if explicitly disabled for this org in `enabled_for_orgs`
    3. Check plan-level defaults
    4. Apply rollout percentage (deterministic hash of org_id)
    """

    key = models.CharField(
        max_length=100,
        unique=True,
        help_text="Unique identifier for the feature flag (e.g., 'feature_x', 'beta_api_v2')",
    )
    description = models.TextField(
        blank=True,
        help_text="Human-readable description of what this flag controls",
    )
    enabled_default = models.BooleanField(
        default=False,
        help_text="Default value if not explicitly set",
    )

    # Plan-level configuration: e.g., {"free": false, "pro": true, "enterprise": true}
    enabled_for_plans = models.JSONField(
        default=dict,
        help_text="Plan-specific enablement: {'free': bool, 'pro': bool, 'enterprise': bool}",
    )

    # Per-org overrides: e.g., {"org_uuid": true, "org_uuid2": false}
    enabled_for_orgs = models.JSONField(
        default=dict,
        help_text="Organization-specific overrides: {org_id_str: true/false}",
    )

    # Rollout control: 0-100 percentage of orgs (deterministic hash-based)
    rollout_pct = models.IntegerField(
        default=0,
        help_text="Percentage (0-100) of orgs for which this flag is enabled (via deterministic hash)",
    )

    # Metadata for internal use
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional metadata (e.g., owner, ticket link, notes)",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Soft-disable the flag without deleting it",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["key"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self):
        return f"{self.key} ({self.rollout_pct}% rollout)"


class ResourceSnapshot(models.Model):
    """
    Immutable snapshots of resources at each version.
    Created automatically via signal handler when a VersionedMixin model is saved.
    
    Supports polymorphic ID types (UUID or BigInteger) for resource_id, organization_id,
    and actor_id. This enables generic snapshots across different ID schemes.
    """

    # Generic relation to any resource
    resource_type = models.CharField(
        max_length=100,
        help_text="Model name (e.g., 'User', 'ApiKey', 'Organization')",
        db_index=True,
    )
    
    # Polymorphic ID fields: support both UUID and integer IDs
    resource_id = PolymorphicIDField(
        help_text="Primary key of the resource (UUID or int)",
        db_index=True,
    )
    organization_id = PolymorphicIDField(
        null=True,
        blank=True,
        help_text="Tenant context for the resource (UUID or int)",
        db_index=True,
    )

    # Version tracking
    version = models.IntegerField(
        help_text="Version number at the time of snapshot",
        db_index=True,
    )

    # Full JSON payload
    data = models.JSONField(
        help_text="Complete JSON representation of the resource at this version",
    )

    # Audit context
    actor_id = PolymorphicIDField(
        null=True,
        blank=True,
        help_text="User ID who triggered the change (UUID or int)",
    )
    request_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Request ID for correlation",
        db_index=True,
    )
    change_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Why the change was made (e.g., 'user_edit', 'admin_action', 'system_migration')",
    )

    # Metadata for tracking snapshot creation context
    snapshot_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional metadata (e.g., request headers, tags, labels)",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["resource_type", "resource_id"]),
            models.Index(fields=["organization_id", "created_at"]),
            models.Index(fields=["resource_type", "version"]),
            models.Index(fields=["resource_type", "organization_id"]),
            models.Index(fields=["actor_id", "created_at"]),
        ]
        verbose_name_plural = "Resource Snapshots"

    def __str__(self):
        return f"{self.resource_type}#{self.resource_id} v{self.version}"

    def to_dict(self) -> dict:
        """Serialize snapshot to dict."""
        return {
            "id": self.id,
            "resource_type": self.resource_type,
            "resource_id": str(self.resource_id) if self.resource_id else None,
            "organization_id": str(self.organization_id) if self.organization_id else None,
            "version": self.version,
            "data": self.data,
            "actor_id": str(self.actor_id) if self.actor_id else None,
            "request_id": self.request_id,
            "change_reason": self.change_reason,
            "created_at": self.created_at.isoformat(),
        }
