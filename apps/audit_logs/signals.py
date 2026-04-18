"""
Django signals that feed audit events from model lifecycle hooks into
the async write_audit_log Celery task.

Models covered
──────────────
  User                  — post_save (created / updated), post_delete
  ApiKey                — post_save, post_delete
  OrganizationMembership — post_save, post_delete

Design notes
────────────
- We connect to post_save / post_delete rather than pre_* so we capture
  the final committed state.
- We use `dispatch_uid` to ensure each signal handler is only connected
  once (important for apps with multiple INSTALLED_APPS load paths).
- All writes are async via Celery, so signals never slow down saves.
"""

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def _fire(action, instance, diff=None):
    """Enqueue a write_audit_log task for the given model instance."""
    try:
        from apps.audit_logs.tasks import write_audit_log

        resource_type = type(instance).__name__
        resource_id = str(instance.pk) if instance.pk else ""

        write_audit_log.delay(
            actor_id=None,  # signals don't have request context
            org_id=_get_org_id(instance),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            diff=diff or {},
        )
    except Exception as exc:
        logger.warning("audit signal failed for %s: %s", type(instance).__name__, exc)


def _get_org_id(instance) -> str | None:
    """Best-effort: extract org_id from the model instance."""
    # Direct org FK
    org_fk = getattr(instance, "org_id", None) or getattr(instance, "organization_id", None)
    if org_fk:
        return str(org_fk)
    # For OrganizationMembership the FK is named organization
    org = getattr(instance, "organization", None)
    if org:
        return str(org.pk)
    return None


# ── User signals ────────────────────────────────────────────────────────────────


@receiver(post_save, sender="users.User", dispatch_uid="audit_user_save")
def on_user_save(sender, instance, created, **kwargs):
    action = "user.created" if created else "user.updated"
    _fire(action, instance, diff={"email": instance.email})


@receiver(post_delete, sender="users.User", dispatch_uid="audit_user_delete")
def on_user_delete(sender, instance, **kwargs):
    _fire("user.deleted", instance, diff={"email": instance.email})


# ── ApiKey signals ──────────────────────────────────────────────────────────────


@receiver(post_save, sender="api_keys.ApiKey", dispatch_uid="audit_apikey_save")
def on_apikey_save(sender, instance, created, **kwargs):
    action = "api_key.created" if created else "api_key.updated"
    _fire(
        action,
        instance,
        diff={
            "name": instance.name,
            "prefix": instance.prefix,
            "is_active": instance.is_active,
        },
    )


@receiver(post_delete, sender="api_keys.ApiKey", dispatch_uid="audit_apikey_delete")
def on_apikey_delete(sender, instance, **kwargs):
    _fire("api_key.deleted", instance, diff={"name": instance.name, "prefix": instance.prefix})


# ── OrganizationMembership signals ──────────────────────────────────────────────


@receiver(
    post_save,
    sender="tenants.OrganizationMembership",
    dispatch_uid="audit_membership_save",
)
def on_membership_save(sender, instance, created, **kwargs):
    action = "membership.added" if created else "membership.updated"
    _fire(
        action,
        instance,
        diff={
            "user_id": str(instance.user_id),
            "role": instance.role,
        },
    )


@receiver(
    post_delete,
    sender="tenants.OrganizationMembership",
    dispatch_uid="audit_membership_delete",
)
def on_membership_delete(sender, instance, **kwargs):
    _fire(
        "membership.removed",
        instance,
        diff={
            "user_id": str(instance.user_id),
            "role": instance.role,
        },
    )
