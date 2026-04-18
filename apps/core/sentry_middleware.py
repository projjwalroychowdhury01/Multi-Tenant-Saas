"""
Middleware for enriching Sentry error scope with tenant and request context.
"""

import logging

from apps.tenants.context import get_current_org
from apps.users.models import User


logger = logging.getLogger(__name__)


class SentryContextMiddleware:
    """
    Middleware to add organization, user, and request context to Sentry errors.
    Only active in production when Sentry is configured.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self._set_sentry_context(request)
        response = self.get_response(request)
        return response

    @staticmethod
    def _set_sentry_context(request):
        """
        Add context to Sentry scope if Sentry is available.
        """
        try:
            import sentry_sdk
        except ImportError:
            return

        # Extract org_id from JWT token or current org
        org_id = None
        user_id = None

        try:
            org_id = getattr(request, "org_id", None) or (
                request.org.id if hasattr(request, "org") else None
            )
        except Exception:
            pass

        # Extract user_id
        if hasattr(request, "user") and request.user.is_authenticated:
            user_id = request.user.id

        # Extract request_id from middleware (if RequestIdMiddleware is installed)
        request_id = getattr(request, "id", None) or request.META.get("X-Request-ID", "")

        # Set Sentry tags and breadcrumb
        with sentry_sdk.push_scope() as scope:
            if org_id:
                scope.set_tag("organization_id", org_id)
            if user_id:
                scope.set_tag("user_id", user_id)
            if request_id:
                scope.set_tag("request_id", request_id)

            # Set user context if available
            if user_id:
                try:
                    user = User.objects.get(id=user_id)
                    scope.set_user(
                        {
                            "id": user.id,
                            "email": user.email,
                        }
                    )
                except Exception:
                    pass
