"""
Celery tasks for the audit_logs app.

write_audit_log
───────────────
Async INSERT into AuditLog.  By keeping the DB write off the request
thread we ensure that:
  - HTTP latency is unaffected even when Postgres is slow.
  - Audit entries are still written even if the view raises an exception
    mid-response (the task was already enqueued).

Sensitive field redaction
─────────────────────────
Any dict key matching REDACTED_FIELDS is replaced with "**REDACTED**"
before the diff is persisted.  The redaction is applied recursively so
it catches nested dicts (e.g., serializer .validated_data payloads).
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

# Fields that must never appear in stored diff payloads
REDACTED_FIELDS = frozenset({
    "password",
    "password1",
    "password2",
    "old_password",
    "new_password",
    "hashed_key",
    "key_hash",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "jwt",
    "api_key",
    "stripe_customer_id",
    "card_number",
    "cvv",
})


def redact_sensitive(data: dict, fields: frozenset = REDACTED_FIELDS) -> dict:
    """
    Recursively replace values for sensitive keys with ``"**REDACTED**"``.

    Operates on plain dicts; lists of dicts are handled element-by-element.
    Non-dict leaves are returned as-is.
    """
    if not isinstance(data, dict):
        return data
    result = {}
    for key, value in data.items():
        if key.lower() in fields:
            result[key] = "**REDACTED**"
        elif isinstance(value, dict):
            result[key] = redact_sensitive(value, fields)
        elif isinstance(value, list):
            result[key] = [
                redact_sensitive(item, fields) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


@shared_task(bind=True, max_retries=3, default_retry_delay=5, ignore_result=True)
def write_audit_log(
    self,
    *,
    actor_id: str | None,
    org_id: str | None,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    diff: dict | None = None,
    ip_address: str | None = None,
    user_agent: str = "",
    request_id: str = "",
) -> None:
    """
    Persist one AuditLog entry.

    Args:
        actor_id:      String UUID of the user performing the action, or None.
        org_id:        String UUID of the tenant, or None for system events.
        action:        Verb string, e.g. "created", "updated", "deleted".
        resource_type: Model class name, e.g. "ApiKey".
        resource_id:   String PK of the affected object.
        diff:          Dict of changed fields; sensitive keys are auto-redacted.
        ip_address:    Remote IP address string.
        user_agent:    HTTP User-Agent header value.
        request_id:    X-Request-ID for log correlation.
    """
    try:
        from apps.audit_logs.models import AuditLog

        clean_diff = redact_sensitive(diff or {})

        AuditLog.objects.create(
            actor_id=actor_id,
            org_id=org_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            diff=clean_diff,
            ip_address=ip_address or None,
            user_agent=user_agent[:512],   # cap to avoid absurdly long UAs
            request_id=request_id[:64],
        )
        logger.debug(
            "audit: %s %s:%s actor=%s org=%s",
            action, resource_type, resource_id, actor_id, org_id,
        )
    except Exception as exc:
        logger.error("write_audit_log failed: %s", exc)
        raise self.retry(exc=exc)
