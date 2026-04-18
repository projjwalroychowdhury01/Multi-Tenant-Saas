"""
API Key management views.

Endpoints
─────────
  POST   /api-keys/              — create key (secret shown ONCE)
  GET    /api-keys/              — list keys (masked)
  GET    /api-keys/{id}/         — retrieve single key (masked)
  PATCH  /api-keys/{id}/         — update name / scopes / expiry / is_active
  DELETE /api-keys/{id}/         — immediately revoke (is_active=False)
  POST   /api-keys/{id}/rotate/  — issue new key; old key expires in 24h

Security model
──────────────
  - All endpoints require authentication (JWT or ApiKey).
  - Create / PATCH / DELETE / rotate require `api_keys:manage` permission.
  - GET requires `api_keys:read` permission.
  - Object-level: users can only manage keys belonging to their org.

Org resolution
──────────────
  DRF authentication runs during view dispatch, *after* Django middleware.
  Therefore TenantContextMiddleware cannot populate request.org from JWT.
  We call _get_request_org() at the start of every view to resolve the org
  from whichever auth method was used:
    - JWT token  → extract org_id claim from request.auth.payload
    - ApiKey     → request.org is already set by ApiKeyAuthentication
"""

import logging
import uuid
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api_keys.authentication import invalidate_api_key_cache
from apps.api_keys.models import ApiKey, EnvChoices
from apps.api_keys.serializers import (
    ApiKeyCreateSerializer,
    ApiKeyListSerializer,
    ApiKeyUpdateSerializer,
)
from apps.rbac.permissions import _get_request_role
from apps.rbac.registry import has_permission

logger = logging.getLogger(__name__)

# How long the old key stays valid after rotation
_ROTATION_OVERLAP_HOURS = 24


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_request_org(request):
    """
    Resolve the Organization for the current request.

    Two possible sources (checked in order):
      1. request.org — set by ApiKeyAuthentication during DRF auth dispatch.
      2. JWT payload  — org_id claim extracted from request.auth.payload.

    Returns the Organization instance or None.
    """
    # Path 1: ApiKeyAuthentication already set it
    org = getattr(request, "org", None)
    if org is not None:
        return org

    # Path 2: JWT token — request.auth is populated by DRF after view dispatch begins
    token_payload = getattr(request.auth, "payload", {}) if request.auth else {}
    org_id = token_payload.get("org_id")
    if not org_id:
        return None

    from apps.tenants.models import Organization

    org = Organization.all_objects.filter(id=org_id, is_active=True).first()
    if org:
        # Cache on request so subsequent calls in the same view are free
        request.org = org
    return org


def _check_permission(request, scope: str):
    """Return (role, error_response) — error_response is None if access is granted."""
    role = _get_request_role(request)
    if not role or not has_permission(role, scope):
        return role, Response(
            {
                "error": f"Your role does not grant the '{scope}' permission.",
                "code": "permission_denied",
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    return role, None


def _resolve_key(org, key_id: str):
    """Resolve an ApiKey belonging to the given org, or return error response."""
    try:
        uid = uuid.UUID(str(key_id))
    except (ValueError, AttributeError):
        return None, Response(
            {"error": "Invalid key ID.", "code": "invalid_id"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        key = ApiKey.all_objects.get(id=uid, organization=org)
    except ApiKey.DoesNotExist:
        return None, Response(
            {"error": "API key not found.", "code": "not_found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return key, None


# ── List + Create ─────────────────────────────────────────────────────────────


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def api_key_list_create(request):
    """
    GET  /api-keys/   — list all non-deleted keys for this org
    POST /api-keys/   — create a new API key
    """
    org = _get_request_org(request)
    if org is None:
        return Response(
            {"error": "No active organisation context.", "code": "no_org"},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == "GET":
        _, err = _check_permission(request, "api_keys:read")
        if err:
            return err

        keys = ApiKey.all_objects.filter(organization=org, is_active=True).order_by("-created_at")
        serializer = ApiKeyListSerializer(keys, many=True)
        return Response({"results": serializer.data, "count": keys.count()})

    # POST
    _, err = _check_permission(request, "api_keys:manage")
    if err:
        return err

    serializer = ApiKeyCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    env = serializer.validated_data.get("env", EnvChoices.LIVE)
    secret = ApiKey.generate_secret(env)
    prefix = ApiKey.derive_prefix(secret)
    hashed = ApiKey.hash_secret(secret)

    api_key = ApiKey.all_objects.create(
        organization=org,
        created_by=request.user,
        prefix=prefix,
        hashed_key=hashed,
        name=serializer.validated_data["name"],
        env=env,
        scopes=serializer.validated_data.get("scopes", []),
        expires_at=serializer.validated_data.get("expires_at"),
    )

    logger.info(
        "API key created: id=%s prefix=%s org=%s by=%s",
        api_key.id,
        prefix,
        org.id,
        request.user.id,
    )

    response_data = ApiKeyListSerializer(api_key).data
    # Inject the plaintext secret — ONLY time it will ever appear
    response_data["secret"] = secret

    return Response(response_data, status=status.HTTP_201_CREATED)


# ── Retrieve + Update + Delete ────────────────────────────────────────────────


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def api_key_detail(request, key_id):
    """
    GET    /api-keys/{id}/  — retrieve (masked)
    PATCH  /api-keys/{id}/  — update name / scopes / expiry
    DELETE /api-keys/{id}/  — revoke immediately
    """
    org = _get_request_org(request)
    if org is None:
        return Response(
            {"error": "No active organisation context.", "code": "no_org"},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == "GET":
        _, err = _check_permission(request, "api_keys:read")
        if err:
            return err
        key, err = _resolve_key(org, key_id)
        if err:
            return err
        return Response(ApiKeyListSerializer(key).data)

    if request.method == "PATCH":
        _, err = _check_permission(request, "api_keys:manage")
        if err:
            return err
        key, err = _resolve_key(org, key_id)
        if err:
            return err

        serializer = ApiKeyUpdateSerializer(key, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        logger.info("API key updated: id=%s org=%s by=%s", key.id, org.id, request.user.id)
        return Response(ApiKeyListSerializer(key).data)

    # DELETE
    _, err = _check_permission(request, "api_keys:manage")
    if err:
        return err
    key, err = _resolve_key(org, key_id)
    if err:
        return err

    key.is_active = False
    key.save(update_fields=["is_active", "updated_at"])
    invalidate_api_key_cache(key.prefix)

    logger.info(
        "API key revoked: id=%s prefix=%s org=%s by=%s", key.id, key.prefix, org.id, request.user.id
    )
    return Response(status=status.HTTP_204_NO_CONTENT)


# ── Rotate ────────────────────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_key_rotate(request, key_id):
    """
    POST /api-keys/{id}/rotate/

    Issues a brand-new key and sets the old key to expire in 24 hours.
    Returns the new plaintext secret ONCE.
    """
    org = _get_request_org(request)
    if org is None:
        return Response(
            {"error": "No active organisation context.", "code": "no_org"},
            status=status.HTTP_403_FORBIDDEN,
        )

    _, err = _check_permission(request, "api_keys:manage")
    if err:
        return err

    old_key, err = _resolve_key(org, key_id)
    if err:
        return err

    if not old_key.is_active:
        return Response(
            {"error": "Cannot rotate a revoked key.", "code": "key_revoked"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Schedule old key to expire after the overlap window
    overlap_expiry = timezone.now() + timedelta(hours=_ROTATION_OVERLAP_HOURS)
    old_key.expires_at = overlap_expiry
    old_key.save(update_fields=["expires_at", "updated_at"])
    # Invalidate cache so revocation propagates within the TTL
    invalidate_api_key_cache(old_key.prefix)

    # Issue the new key (same env + scopes as the old one)
    secret = ApiKey.generate_secret(old_key.env)
    prefix = ApiKey.derive_prefix(secret)
    hashed = ApiKey.hash_secret(secret)

    new_key = ApiKey.all_objects.create(
        organization=org,
        created_by=request.user,
        prefix=prefix,
        hashed_key=hashed,
        name=f"{old_key.name} (rotated)",
        env=old_key.env,
        scopes=old_key.scopes,
    )

    logger.info(
        "API key rotated: old=%s new=%s org=%s by=%s overlap_until=%s",
        old_key.id,
        new_key.id,
        org.id,
        request.user.id,
        overlap_expiry,
    )

    response_data = ApiKeyListSerializer(new_key).data
    response_data["secret"] = secret
    response_data["old_key_expires_at"] = overlap_expiry

    return Response(response_data, status=status.HTTP_201_CREATED)
