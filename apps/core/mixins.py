"""
Core shared mixins — all other models build on these.

Provides:
  - TimeStampedModel   : auto created_at / updated_at
  - SoftDeleteMixin    : soft-delete with .alive() / .deleted() manager methods
  - VersionedMixin     : monotonically incrementing version field per record
"""

import uuid

from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """Abstract base that auto-populates created_at and updated_at."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ── Soft Delete ────────────────────────────────────────────────────────────────


class SoftDeleteQuerySet(models.QuerySet):
    """QuerySet with .alive() and .deleted() filter helpers."""

    def alive(self):
        """Return only non-deleted records (default behaviour)."""
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        """Return only soft-deleted records."""
        return self.filter(deleted_at__isnull=False)


class SoftDeleteManager(models.Manager):
    """Default manager that excludes soft-deleted records."""

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).alive()

    def alive(self):
        return self.get_queryset().alive()

    def deleted(self):
        return SoftDeleteQuerySet(self.model, using=self._db).deleted()

    def all_including_deleted(self):
        return SoftDeleteQuerySet(self.model, using=self._db)


class SoftDeleteMixin(models.Model):
    """
    Mixin that prevents hard deletes.

    Usage:
        obj.soft_delete(deleted_by=request.user)
        MyModel.objects.alive()    # excludes deleted
        MyModel.objects.deleted()  # only deleted
        MyModel.all_objects.all()  # bypass filter entirely
    """

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    def soft_delete(self, deleted_by=None):
        """Mark the record as deleted without issuing a DELETE statement."""
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by
        self.save(update_fields=["deleted_at", "deleted_by", "updated_at"])

    def restore(self):
        """Undo a soft delete."""
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["deleted_at", "deleted_by", "updated_at"])

    class Meta:
        abstract = True


# ── Data Versioning ────────────────────────────────────────────────────────────


class VersionedMixin(models.Model):
    """
    Increments a version counter on every save.
    Used together with ResourceSnapshot to maintain full history.
    """

    version = models.PositiveIntegerField(default=1)

    def save(self, *args, **kwargs):
        if self.pk:
            # Atomic increment — avoids race conditions on concurrent saves
            type(self).all_objects.filter(pk=self.pk).update(
                version=models.F("version") + 1
            )
            self.refresh_from_db(fields=["version"])
        super().save(*args, **kwargs)

    class Meta:
        abstract = True
