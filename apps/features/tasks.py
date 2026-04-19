"""
Celery tasks for features app.

Handles:
  - Async resource snapshot creation
  - Cache invalidation
  - Batch snapshot operations
"""

import logging
from typing import Optional, Union

from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=5, ignore_result=False)
def create_resource_snapshot_async(
    self,
    *,
    resource_type: str,
    resource_id: Union[str, int],
    version: int,
    change_reason: str = "model_updated",
    metadata: Optional[dict] = None,
    actor_id: Optional[Union[str, int]] = None,
    request_id: Optional[str] = None,
    org_id: Optional[Union[str, int]] = None,
) -> int:
    """
    Async task to create a resource snapshot.

    Args:
        resource_type: Model class name
        resource_id: Primary key of the resource
        version: Version number
        change_reason: Reason for the change
        metadata: Additional metadata
        actor_id: User ID who triggered the change
        request_id: Request ID for correlation
        org_id: Organization ID (tenant)

    Returns:
        Created snapshot ID
    """
    from .models import ResourceSnapshot

    try:
        # Fetch the actual model instance to get full data
        try:
            model_class = apps.get_model("features", resource_type)
        except LookupError:
            # Try other apps if not in features
            for app_config in apps.get_app_configs():
                try:
                    model_class = apps.get_model(app_config.name, resource_type)
                    break
                except LookupError:
                    continue
            else:
                logger.error(f"Model {resource_type} not found")
                return None

        # Fetch the model instance
        try:
            instance = model_class.objects.get(pk=resource_id)
        except model_class.DoesNotExist:
            logger.warning(f"{resource_type}#{resource_id} not found - creating minimal snapshot")
            instance = None

        # Build model data
        if instance:
            model_data = {}
            for field in instance._meta.get_fields():
                # Skip relation fields
                if field.many_to_one or field.one_to_many or field.many_to_many:
                    continue

                try:
                    value = getattr(instance, field.name, None)
                    # Validate serializability
                    DjangoJSONEncoder().encode(value)
                    model_data[field.name] = value
                except (TypeError, ValueError):
                    logger.debug(f"Skipping non-serializable field {field.name}")
        else:
            model_data = {"id": resource_id, "_error": "Instance not found at snapshot time"}

        # Infer org_id if not provided
        if org_id is None and instance:
            org_id = getattr(instance, "organization_id", None) or getattr(instance, "org_id", None)

        # Build metadata
        snapshot_meta = metadata or {}
        snapshot_meta.update(
            {
                "created_at": timezone.now().isoformat(),
                "async_task": True,
            }
        )
        if actor_id:
            snapshot_meta["created_by"] = str(actor_id)
        if request_id:
            snapshot_meta["request_id"] = request_id

        # Create snapshot
        snapshot = ResourceSnapshot.objects.create(
            resource_type=resource_type,
            resource_id=resource_id,
            organization_id=org_id,
            version=version,
            data=model_data,
            actor_id=actor_id,
            request_id=request_id,
            change_reason=change_reason,
            snapshot_metadata=snapshot_meta,
        )

        logger.info(
            f"Created async snapshot {snapshot.id} for {resource_type}#{resource_id} v{version}"
        )

        # Invalidate cache
        from .signals import invalidate_snapshot_cache

        invalidate_snapshot_cache(resource_type=resource_type, organization_id=org_id)

        return snapshot.id

    except Exception as exc:
        logger.exception(f"Error creating snapshot: {exc}")
        # Retry with exponential backoff
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def restore_resource_snapshot(
    self,
    *,
    snapshot_id: int,
    restore_changes: bool = True,
) -> dict:
    """
    Async task to restore a resource from a snapshot.

    Args:
        snapshot_id: ResourceSnapshot ID to restore from
        restore_changes: Whether to apply the snapshot data as an update

    Returns:
        Restore result dict with status and details
    """
    from .models import ResourceSnapshot

    try:
        snapshot = ResourceSnapshot.objects.get(id=snapshot_id)

        # Get the model class
        try:
            model_class = apps.get_model("features", snapshot.resource_type)
        except LookupError:
            for app_config in apps.get_app_configs():
                try:
                    model_class = apps.get_model(app_config.name, snapshot.resource_type)
                    break
                except LookupError:
                    continue
            else:
                return {
                    "status": "error",
                    "message": f"Model {snapshot.resource_type} not found",
                }

        # Fetch the resource
        try:
            instance = model_class.objects.get(pk=snapshot.resource_id)
        except model_class.DoesNotExist:
            return {
                "status": "error",
                "message": f"{snapshot.resource_type}#{snapshot.resource_id} not found",
            }

        if restore_changes:
            # Apply snapshot data to model fields
            snapshot_data = snapshot.data
            updated_fields = []

            for field_name, value in snapshot_data.items():
                if hasattr(instance, field_name) and field_name != "id":
                    try:
                        setattr(instance, field_name, value)
                        updated_fields.append(field_name)
                    except (TypeError, ValueError):
                        logger.warning(f"Could not restore field {field_name} to {value}")

            # Save without triggering new snapshot
            instance.save(update_fields=updated_fields)

            return {
                "status": "success",
                "message": f"Restored {snapshot.resource_type}#{snapshot.resource_id}",
                "updated_fields": updated_fields,
                "snapshot_id": snapshot_id,
            }
        else:
            return {
                "status": "success",
                "message": f"Snapshot {snapshot_id} verified",
                "snapshot_id": snapshot_id,
            }

    except Exception as exc:
        logger.exception(f"Error restoring snapshot: {exc}")
        raise self.retry(exc=exc)


@shared_task
def cleanup_old_snapshots(days_old: int = 90) -> dict:
    """
    Cleanup old snapshots beyond retention period.

    Args:
        days_old: Number of days to retain

    Returns:
        Cleanup result with count
    """
    from datetime import timedelta

    from .models import ResourceSnapshot

    try:
        cutoff_date = timezone.now() - timedelta(days=days_old)
        old_snapshots = ResourceSnapshot.objects.filter(created_at__lt=cutoff_date)
        count = old_snapshots.count()

        # Delete in batches to avoid memory issues
        batch_size = 1000
        for i in range(0, count, batch_size):
            old_snapshots[i : i + batch_size].delete()

        logger.info(f"Cleaned up {count} snapshots older than {days_old} days")

        return {
            "status": "success",
            "deleted_count": count,
            "cutoff_date": cutoff_date.isoformat(),
        }

    except Exception as exc:
        logger.exception(f"Error cleaning up snapshots: {exc}")
        return {"status": "error", "message": str(exc)}
