"""
TenantContextMiddleware — injects the current organisation into thread-local
storage so that TenantManager can auto-filter all ORM queries.

This middleware runs AFTER JWTAuthMiddleware which sets request.org.
It reads request.org (set by the authentication layer) and calls
set_current_org() so every subsequent ORM call in this thread gets the
correct tenant filter applied automatically.

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
        # Bind the org resolved by the auth middleware (may be None for
        # unauthenticated requests — TenantManager handles None gracefully).
        org = getattr(request, "org", None)
        set_current_org(org)

        try:
            response = self.get_response(request)
        finally:
            # Non-negotiable: always clear the context so threads don't bleed.
            clear_current_org()

        return response
