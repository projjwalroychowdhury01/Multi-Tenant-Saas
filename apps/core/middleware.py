"""
Core middleware.

RequestIdMiddleware  — injects a unique X-Request-ID UUID into every
                       request so that log lines, audit entries, and Sentry
                       events can all be correlated back to a single HTTP
                       transaction.

RateLimitMiddleware  — enforces per-tenant, per-plan sliding-window rate
                       limits.  Injects X-RateLimit-* response headers on
                       every authenticated request and returns HTTP 429 with
                       a Retry-After header when the tenant is over-limit.
"""

import logging
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


class RequestIdMiddleware(MiddlewareMixin):
    """
    Generates a UUID4 request ID and attaches it as:
      - request.request_id  (available to views)
      - X-Request-ID response header (visible to clients / load balancers)
    """

    HEADER = "HTTP_X_REQUEST_ID"
    RESPONSE_HEADER = "X-Request-ID"

    def process_request(self, request):
        # Honour a pre-existing request-id forwarded by an upstream proxy
        request_id = request.META.get(self.HEADER) or str(uuid.uuid4())
        request.request_id = request_id

    def process_response(self, request, response):
        request_id = getattr(request, "request_id", str(uuid.uuid4()))
        response[self.RESPONSE_HEADER] = request_id
        return response


class RateLimitMiddleware:
    """
    Per-tenant sliding-window rate limiter.

    Runs after TenantContextMiddleware so request.org is already populated.
    Only applies to authenticated requests that have a resolved org — public
    endpoints (e.g. /auth/register, /health) are unaffected.

    Response headers injected on EVERY authenticated request:
      X-RateLimit-Limit     — plan limit (requests / minute)
      X-RateLimit-Remaining — requests remaining in current window
      X-RateLimit-Reset     — Unix timestamp of window reset

    When the limit is exceeded:
      HTTP 429 + Retry-After: <seconds>
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(settings, "RATE_LIMIT_ENABLED", True) is False:
            return self.get_response(request)

        org = getattr(request, "org", None)
        if org is None:
            # Unauthenticated or no tenant — skip rate limiting
            return self.get_response(request)

        from apps.core.rate_limit import check_rate_limit

        result = check_rate_limit(str(org.id), org.plan_slug)

        if not result.allowed:
            logger.warning(
                "Rate limit exceeded: org=%s plan=%s count=%s limit=%s",
                org.id,
                org.plan_slug,
                result.current_count,
                result.limit,
            )
            resp = JsonResponse(
                {
                    "error": "Rate limit exceeded. Please slow down.",
                    "code": "rate_limit_exceeded",
                    "retry_after": result.retry_after,
                },
                status=429,
            )
            resp["X-RateLimit-Limit"] = str(result.limit)
            resp["X-RateLimit-Remaining"] = "0"
            resp["X-RateLimit-Reset"] = str(result.reset_at)
            resp["Retry-After"] = str(result.retry_after)
            return resp

        response = self.get_response(request)

        # Inject headers on every allowed authenticated response
        response["X-RateLimit-Limit"] = str(result.limit)
        response["X-RateLimit-Remaining"] = str(result.remaining)
        response["X-RateLimit-Reset"] = str(result.reset_at)

        return response
