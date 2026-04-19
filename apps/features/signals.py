"""
Django signals for automatic resource snapshot creation.

Handles:
  - post_save on VersionedMixin models → create ResourceSnapshot
  - post_delete on models → create deletion snapshot
  - Cache invalidation on snapshot changes
"""

import json
import logging
from typing import Any, Optional

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.core.mixins import SoftDeleteMixin, VersionedMixin
from apps.tenants.context import get_current_org

from .cache import VersionedCacheNamespace
from .models import ResourceSnapshot
from .tasks import create_resource_snapshot_async

logger = logging.getLogger(__name__)

# Cache for versioned snapshots
_snapshot_cache = VersionedCacheNamespace("snapshot", ttl=3600)


def _extract_model_data(instance: Any) -> dict:
    """
    Extract serializable JSON data from a model instance.
    Handles UUID, datetime, and other common field types.
    """
    from django.core.serializers.json import DjangoJSONEncoder
    from django.db.models import Model

    if not isinstance(instance, Model):
        return {}

    data = {}
    for field in instance._meta.get_fields():
        # Skip relation fields
        if field.many_to_one or field.one_to_many or field.many_to_many:
            continue

        try:
            value = getattr(instance, field.name, None)
            # Use DjangoJSONEncoder for safe serialization
            json.dumps(value, cls=DjangoJSONEncoder)
            data[field.name] = value
        except (TypeError, ValueError):
            # Skip unserializable fields
            logger.debug(f"Skipping field {field.name} - not serializable")

    return data


def _get_audit_context() -> dict:
    """Extract current audit context (actor_id, request_id, org_id)."""


    # Try to get from thread-local storage (set by middleware)
    try:
        from apps.audit_logs.middleware import _request_context

        context = _request_context.value if hasattr(_request_context, "value") else {}
    except (ImportError, AttributeError):
        context = {}

    org = get_current_org()

    return {
        "actor_id": context.get("actor_id") if context else None,
        "request_id": context.get("request_id") if context else None,
        "org_id": org.id if org else None,
    }


def _create_snapshot_from_instance(
    instance: Any,
    change_reason: str = "unknown",
    metadata: Optional[dict] = None,
) -> ResourceSnapshot:
    """
    Create a ResourceSnapshot from a model instance.

    Args:
        instance: Model instance to snapshot
        change_reason: Reason for the change
        metadata: Additional metadata to store

    Returns:
        Created ResourceSnapshot instance
    """
    audit_context = _get_audit_context()

    # Extract model data
    model_data = _extract_model_data(instance)

    # Get org_id from the model if available
    org_id = audit_context["org_id"]
    if org_id is None:
        org_id = getattr(instance, "organization_id", None) or getattr(instance, "org_id", None)

    # Get version from VersionedMixin if available
    version = getattr(instance, "version", 1)

    # Build snapshot metadata
    snapshot_meta = metadata or {}
    snapshot_meta.update(
        {
            "created_by": audit_context["actor_id"],
            "request_id": audit_context["request_id"],
            "timestamp_utc": timezone.now().isoformat(),
        }
    )

    # Create snapshot
    snapshot = ResourceSnapshot.objects.create(
        resource_type=instance.__class__.__name__,
        resource_id=instance.pk,
        organization_id=org_id,
        version=version,
        data=model_data,
        actor_id=audit_context["actor_id"],
        request_id=audit_context["request_id"],
        change_reason=change_reason,
        snapshot_metadata=snapshot_meta,
    )

    logger.info(
        f"Created snapshot {snapshot.id} for {snapshot.resource_type}#{snapshot.resource_id} v{version}"
    )

    return snapshot


@receiver(post_save, dispatch_uid="snapshot_versioned_model_save")
def on_versioned_model_save(sender, instance, created, **kwargs):
    """
    Signal handler: Create snapshot after any VersionedMixin model is saved.

    Uses async task to avoid blocking the save response.
    """
    # Only listen to VersionedMixin models
    if not isinstance(instance, VersionedMixin):
        return

    # Skip if this is the initial creation and version is still 1
    # (snapshots are created on updates, not initial creation)
    version = getattr(instance, "version", 1)
    if created and version == 1:
        logger.debug(f"Skipping snapshot for new {sender.__name__} - version 1")
        return

    # Enqueue async snapshot creation
    try:
        create_resource_snapshot_async.delay(
            resource_type=sender.__name__,
            resource_id=instance.pk,
            version=version,
            change_reason="model_updated",
        )
        logger.debug(f"Enqueued snapshot task for {sender.__name__}#{instance.pk}")
    except Exception as exc:
        logger.exception(f"Failed to enqueue snapshot task: {exc}")
        # Fall back to synchronous creation
        try:
            _create_snapshot_from_instance(instance, "model_updated")
        except Exception as exc2:
            logger.exception(f"Failed to create snapshot synchronously: {exc2}")


@receiver(post_delete, dispatch_uid="snapshot_soft_delete_capture")
def on_model_delete(sender, instance, **kwargs):
    """
    Signal handler: Capture deletion snapshots for SoftDeleteMixin models.

    Creates a snapshot with change_reason='deleted' to preserve the final state.
    """
    # Only listen to SoftDeleteMixin models (soft deletes)
    if not isinstance(instance, SoftDeleteMixin):
        return

    # Only capture if actually soft-deleted (has deleted_at)
    if not getattr(instance, "deleted_at", None):
        return

    try:
        # Add deletion metadata
        metadata = {
            "deletion_type": "soft_delete",
            "deleted_at": instance.deleted_at.isoformat() if instance.deleted_at else None,
            "deleted_by": getattr(instance, "deleted_by_id", None),
        }

        _create_snapshot_from_instance(instance, "deleted", metadata=metadata)
    except Exception as exc:
        logger.exception(f"Failed to create deletion snapshot: {exc}")


def invalidate_snapshot_cache(
    resource_type: Optional[str] = None,
    organization_id: Optional[Any] = None,
) -> int:
    """
    Invalidate snapshot cache entries.

    Args:
        resource_type: Resource type to invalidate
        organization_id: Organization to invalidate

    Returns:
        Number of cache entries invalidated
    """
    count = 0

    if resource_type:
        count += _snapshot_cache.invalidate_entity(resource_type)

    if organization_id:
        count += _snapshot_cache.invalidate_org(organization_id)

    if not resource_type and not organization_id:
        _snapshot_cache.invalidate_namespace()
        count = 1

    return count
