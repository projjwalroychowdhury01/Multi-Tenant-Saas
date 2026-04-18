"""
Custom DRF authentication class for API key Bearer tokens.

Flow
────
  1. Client sends: Authorization: Bearer sk_live_4f3a<rest-of-secret>
  2. We extract the prefix (first 12 chars of the full token, e.g. 'sk_live_4f3a').
  3. Lookup ApiKey by prefix in Redis (TTL=60s) → cache miss falls through to DB.
  4. Re-hash the submitted secret; compare against stored hashed_key.
  5. Validate key is active and not expired.
  6. Set request.user = key.created_by, request.org = key.organization.
  7. Fire-and-forget Celery task to update last_used_at.

Redis cache key format
──────────────────────
  api_key:prefix:{prefix}  →  {hashed_key}|{org_id}|{key_id}

This avoids a DB hit on every API request while keeping cache invalidation
simple (delete the cache entry on revoke/rotate).
"""

import logging

from django.core.cache import cache
from django.utils import timezone
from rest_framework import authentication, exceptions

from apps.api_keys.models import ApiKey

logger = logging.getLogger(__name__)

# Cache TTL for api_key lookups — short so revocations propagate quickly.
_CACHE_TTL = 60  # seconds
_CACHE_PREFIX = "api_key:prefix:"


def _cache_key(prefix: str) -> str:
    return f"{_CACHE_PREFIX}{prefix}"


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """
    Authenticate requests that carry an API key in the Authorization header.

    The JWT auth class is tried second; this class is tried first (ordering
    in DEFAULT_AUTHENTICATION_CLASSES matters — see settings/base.py).

    authenticate_header() returning a non-empty string signals to DRF that
    this backend participates in WWW-Authenticate challenges, which causes
    DRF to return HTTP 401 (not 403) for unauthenticated requests.
    """

    keyword = "Bearer"

    def authenticate_header(self, request) -> str:
        """
        Return a value for the WWW-Authenticate header on 401 responses.

        DRF uses the presence of this method (returning a non-empty string)
        to decide whether to return HTTP 401 or HTTP 403 for unauthenticated
        requests.  Without this, DRF sends 403 even when no credentials are
        provided, which breaks any client that expects 401 to trigger
        automatic re-authentication.
        """
        return self.keyword

    def authenticate(self, request):
        auth_header = authentication.get_authorization_header(request).decode(
            "utf-8", errors="ignore"
        )

        if not auth_header:
            return None  # Let the next auth class try

        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != self.keyword.lower():
            return None

        raw_token = parts[1]

        # Only handle sk_live_ / sk_test_ tokens — skip JWT-style tokens
        if not (raw_token.startswith("sk_live_") or raw_token.startswith("sk_test_")):
            return None

        return self._authenticate_with_key(request, raw_token)

    def _authenticate_with_key(self, request, secret: str):
        """Core verification logic: prefix lookup → hash compare → liveness check."""
        try:
            prefix = ApiKey.derive_prefix(secret)
        except ValueError:
            raise exceptions.AuthenticationFailed("Malformed API key.")

        # ── Redis cache lookup ────────────────────────────────────────────────
        cached = cache.get(_cache_key(prefix))
        if cached:
            hashed_key, org_id, key_id = cached.split("|", 2)
            if not ApiKey.verify_secret(secret, hashed_key):
                raise exceptions.AuthenticationFailed("Invalid API key.")
            # Fetch the full key object for org resolution (lightweight)
            try:
                api_key = ApiKey.all_objects.select_related("organization", "created_by").get(
                    id=key_id, is_active=True
                )
            except ApiKey.DoesNotExist:
                cache.delete(_cache_key(prefix))
                raise exceptions.AuthenticationFailed("API key has been revoked.")
        else:
            # ── DB lookup ──────────────────────────────────────────────────────
            try:
                api_key = ApiKey.all_objects.select_related("organization", "created_by").get(
                    prefix=prefix, is_active=True
                )
            except ApiKey.DoesNotExist:
                raise exceptions.AuthenticationFailed("Invalid API key.")

            if not ApiKey.verify_secret(secret, api_key.hashed_key):
                raise exceptions.AuthenticationFailed("Invalid API key.")

            # Populate cache for subsequent requests
            cache.set(
                _cache_key(prefix),
                f"{api_key.hashed_key}|{api_key.organization_id}|{api_key.id}",
                timeout=_CACHE_TTL,
            )

        # ── Liveness checks ───────────────────────────────────────────────────
        if api_key.expires_at and api_key.expires_at < timezone.now():
            cache.delete(_cache_key(prefix))
            raise exceptions.AuthenticationFailed("API key has expired.")

        # ── Attach tenant context to the request ──────────────────────────────
        request.org = api_key.organization
        request.api_key = api_key

        # ── Async update of last_used_at (non-blocking) ───────────────────────
        try:
            from apps.api_keys.tasks import update_api_key_last_used

            update_api_key_last_used.delay(str(api_key.id))
        except Exception:  # pragma: no cover — Celery not available in all envs
            logger.warning("Could not schedule last_used_at update for key %s", api_key.id)

        logger.info(
            "API key auth: key_id=%s org=%s",
            api_key.id,
            api_key.organization_id,
        )

        # Return (user, auth_token) — user may be None for truly anonymous API keys
        return (api_key.created_by, api_key)


def invalidate_api_key_cache(prefix: str) -> None:
    """Call this when a key is revoked or rotated to clear the Redis cache entry."""
    cache.delete(_cache_key(prefix))
