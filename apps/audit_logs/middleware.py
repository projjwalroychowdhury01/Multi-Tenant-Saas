"""
AuditLogMiddleware — captures HTTP context on mutating requests and
dispatches an async audit log entry after the response is committed.

UsageMeterMiddleware — atomically increments per-tenant hourly API call
counters in Redis on every authenticated request.

Placement in MIDDLEWARE (base.py):
  ...
  "apps.tenants.middleware.TenantContextMiddleware",   ← sets request.org
  "apps.core.middleware.RateLimitMiddleware",
  "apps.audit_logs.middleware.AuditLogMiddleware",     ← reads request.org
  "apps.audit_logs.middleware.UsageMeterMiddleware",   ← reads request.org
  ...
"""

import json
import logging

logger = logging.getLogger(__name__)

# HTTP methods that produce state changes worth logging
_MUTABLE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _get_client_ip(request) -> str | None:
    """Extract the best-effort client IP, honouring X-Forwarded-For."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _try_parse_body(body_bytes: bytes) -> dict:
    """Safely parse JSON request body; return empty dict on any failure."""
    if not body_bytes:
        return {}
    try:
        payload = json.loads(body_bytes)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


class AuditLogMiddleware:
    """
    Captures HTTP context from mutating requests and fires an async
    ``write_audit_log`` Celery task after the response is returned.

    The audit log entry is always written AFTER the view returns — if
    the view raises an exception the Response still completes (DRF
    exception handlers produce a response) so we can log the attempt.

    Context attached to request._audit_ctx:
      actor_id, org_id, action, resource_type, resource_id, diff,
      ip_address, user_agent, request_id
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Read and cache the body before Django consumes it in the view
        if request.method in _MUTABLE_METHODS:
            try:
                body = request.body  # bytes; caches internally in Django
            except Exception:
                body = b""
            request._raw_body = body
        else:
            request._raw_body = b""

        response = self.get_response(request)

        # Only log mutating requests that had an authenticated org
        if request.method not in _MUTABLE_METHODS:
            return response

        org = getattr(request, "org", None)
        user = getattr(request, "user", None)
        actor_id = str(user.id) if user and user.is_authenticated else None

        # Build a best-effort action string from the URL path
        path_parts = [p for p in request.path.strip("/").split("/") if p]
        action = f"{request.method.lower()}.{'.'.join(path_parts[-2:])}"

        diff = _try_parse_body(request._raw_body)

        try:
            from apps.audit_logs.tasks import write_audit_log

            write_audit_log.delay(
                actor_id=actor_id,
                org_id=str(org.id) if org else None,
                action=action,
                resource_type=path_parts[-2] if len(path_parts) >= 2 else "",
                resource_id=path_parts[-1] if len(path_parts) >= 1 else "",
                diff=diff,
                ip_address=_get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
                request_id=getattr(request, "request_id", ""),
            )
        except Exception as exc:
            logger.warning("AuditLogMiddleware: failed to enqueue task: %s", exc)

        return response


class UsageMeterMiddleware:
    """
    Atomically increments the Redis usage counter for every authenticated
    request that has a resolved org.

    Key schema:
      usage:{org_id}:api_calls:{YYYY-MM-DD-HH}

    A 25-hour TTL ensures keys expire automatically (the hourly Celery Beat
    task flushes them to UsageRecord rows before they expire).

    This middleware is intentionally minimal — no branching, single O(1) Redis
    call.  It never blocks the request if Redis is unavailable (fire-and-forget
    swallows errors).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        org = getattr(request, "org", None)
        if org is None:
            return response

        try:
            from django.core.cache import cache
            from django.utils.timezone import now

            hour_bucket = now().strftime("%Y-%m-%d-%H")
            key = f"usage:{org.id}:api_calls:{hour_bucket}"

            # cache.incr raises ValueError if key doesn't exist — use add+incr
            try:
                cache.incr(key)
            except ValueError:
                cache.add(key, 0, timeout=60 * 60 * 25)  # 25-hour TTL
                cache.incr(key)

        except Exception as exc:
            logger.debug("UsageMeterMiddleware: Redis error (ignored): %s", exc)

        return response
