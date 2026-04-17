from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone


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
    """

    # Generic relation to any resource
    resource_type = models.CharField(
        max_length=100,
        help_text="Model name (e.g., 'User', 'ApiKey', 'Organization')",
    )
    resource_id = models.BigIntegerField(
        help_text="Primary key of the resource",
    )
    organization_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Tenant context for the resource",
    )

    # Version tracking
    version = models.IntegerField(
        help_text="Version number at the time of snapshot",
    )

    # Full JSON payload
    data = models.JSONField(
        help_text="Complete JSON representation of the resource at this version",
    )

    # Audit context
    actor_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="User ID who triggered the change",
    )
    request_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Request ID for correlation",
    )
    change_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Why the change was made (e.g., 'user_edit', 'admin_action', 'system_migration')",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["resource_type", "resource_id"]),
            models.Index(fields=["organization_id", "created_at"]),
            models.Index(fields=["resource_type", "version"]),
        ]

    def __str__(self):
        return f"{self.resource_type}#{self.resource_id} v{self.version}"
