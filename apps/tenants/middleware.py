"""
TenantContextMiddleware — injects the current organisation into thread-local
storage so that TenantManager can auto-filter all ORM queries.

JWT flow:
  The JWTAuthentication class (configured in DEFAULT_AUTHENTICATION_CLASSES)
  decodes the token and populates request.user.  The `org_id` claim in the
  token is used by this middleware to resolve request.org.

API key flow (Phase 3):
  ApiKeyAuthentication sets request.org directly during authentication because
  it resolves the org from the key itself (no token claim needed).  This
  middleware detects that request.org is already set and skips the DB lookup.

CRITICAL SAFETY NOTE:
  The `finally` block in __call__ ALWAYS clears the tenant context,
  even if the view raises an unhandled exception. Without this,
  a thread-pool server (e.g., Gunicorn threaded mode) would reuse the same
  OS thread for the next request — carrying forward the previous tenant's
  context and causing a data leak.
"""

from apps.tenants.context import clear_current_org, set_current_org


class TenantContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # ApiKeyAuthentication sets request.org directly before middleware runs.
        # JWT flow: request.org is set below via token claim resolution.
        # Both paths converge here — we just bind whichever org was resolved.
        org = getattr(request, "org", None)

        # JWT path: resolve org from token claims if not already set by API key auth.
        # This runs lazily on first access in views via DRF's request wrapper,
        # but we need org in thread-local BEFORE the view executes.
        if org is None:
            org = _resolve_org_from_jwt(request)
            if org is not None:
                request.org = org

        set_current_org(org)

        try:
            response = self.get_response(request)
        finally:
            # Non-negotiable: always clear the context so threads don't bleed.
            clear_current_org()

        return response


def _resolve_org_from_jwt(request):
    """
    Extract org_id from the JWT payload and resolve the Organization instance.

    Returns None on any failure (unauthenticated, missing claim, deleted org).
    """
    try:
        from rest_framework_simplejwt.authentication import JWTAuthentication
        from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken

        try:
            auth_tuple = JWTAuthentication().authenticate(request)
        except (AuthenticationFailed, InvalidToken):
            return None
            
        if auth_tuple is None:
            return None
            
        _, token = auth_tuple
        org_id = token.payload.get("org_id")
        
        if not org_id:
            return None

        from apps.tenants.models import Organization
        return Organization.all_objects.filter(id=org_id, is_active=True).first()
    except Exception:
        return None
