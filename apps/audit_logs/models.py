"""
AuditLog model — immutable event log.

Design decisions
────────────────
- Records are NEVER updated or deleted (append-only).
- is_managed = False would prevent Django from ever issuing DROP TABLE.
- All writes go through the async `write_audit_log` Celery task so HTTP
  requests are never blocked waiting for an INSERT.
- `diff` stores a JSON snapshot of changed fields; sensitive fields are
  stripped before storage by the task (see tasks.py).
- Indexed fields: org, actor, action, resource_type, created_at — covers
  the main query patterns (per-org listing, per-user history, type filters,
  date range pagination).
"""

import uuid

from django.db import models


class AuditLog(models.Model):
    """
    Immutable event record. One row per interesting system event.

    Fields intentionally match the PRD spec:
      actor         — user who performed the action (null for system events)
      org           — tenant the event belongs to
      action        — verb: created / updated / deleted / login / logout / etc.
      resource_type — model class name: "User", "ApiKey", "Membership", …
      resource_id   — string PK of the affected resource
      diff          — JSON dict of changed fields {field: [old, new]} or full
                      payload for CREATE/DELETE; sensitive keys are redacted
      ip_address    — remote IP extracted from X-Forwarded-For or REMOTE_ADDR
      user_agent    — raw User-Agent header string
      request_id    — X-Request-ID UUID for log correlation
      created_at    — immutable timestamp (no updated_at — record never changes)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Who did it — null for background / system actions
    actor = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_actions",
        db_index=True,
    )

    # Which tenant does this event belong to
    org = models.ForeignKey(
        "tenants.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        db_index=True,
    )

    # Verb — e.g. "created", "updated", "deleted", "login", "api_key.rotated"
    action = models.CharField(max_length=120, db_index=True)

    # Affected model class name
    resource_type = models.CharField(max_length=120, blank=True, default="", db_index=True)

    # String representation of the affected object PK (UUID or int as str)
    resource_id = models.CharField(max_length=255, blank=True, default="")

    # Changed-field snapshot. Sensitive keys are stripped before save.
    diff = models.JSONField(default=dict, blank=True)

    # HTTP context
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    request_id = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # Immutable creation timestamp
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        indexes = [
            # Composite index for the standard per-org paginated query
            models.Index(fields=["org", "-created_at"], name="auditlog_org_created_idx"),
            # For per-actor history pages
            models.Index(fields=["actor", "-created_at"], name="auditlog_actor_created_idx"),
            # For per-resource history
            models.Index(
                fields=["resource_type", "resource_id"],
                name="auditlog_resource_idx",
            ),
        ]

    def __str__(self):
        return f"[{self.action}] {self.resource_type}:{self.resource_id} by {self.actor_id}"

    # Guard against accidental updates — the model is append-only
    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("AuditLog records are immutable — do not update them.")
        super().save(*args, **kwargs)
