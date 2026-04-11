"""
TenantManager — the cornerstone of row-level tenant isolation.

Any model that inherits TenantModel gets this manager as its default
`objects` manager.  Every call to `.objects.get_queryset()` automatically
applies `.filter(organization=get_current_org())`, so developers never
need to remember to add tenant filters manually.

The `all_objects` = models.Manager() escape hatch on TenantModel bypasses
this filtering and should only be used in:
  - Django migrations
  - Django Admin views
  - Celery tasks that explicitly operate across tenants
"""

from django.db import models

from apps.tenants.context import get_current_org


class TenantManager(models.Manager):
    """
    Default manager for all tenant-scoped models.

    Auto-filters every queryset to the current request's organization.
    Raises no error if get_current_org() is None — callers using
    `all_objects` bypass this manager entirely.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        org = get_current_org()
        if org is not None:
            qs = qs.filter(organization=org)
        return qs
