"""
Idempotency key handling for billing mutations.

Ensures that retried requests produce the same result as the first attempt,
preventing duplicate charges or subscriptions.

Usage in views:
  from apps.billing.idempotency import IdempotencyMiddleware, ensure_idempotency
  
  @ensure_idempotency("subscribe")
  @api_view(["POST"])
  def subscribe(request):
      ...
"""

import hashlib
import logging
from datetime import timedelta
from typing import Optional, Tuple

from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

logger = logging.getLogger(__name__)

# Cache prefix for idempotency keys
IDEMPOTENCY_CACHE_PREFIX = "idempotency:"
IDEMPOTENCY_CACHE_TTL = 86400  # 24 hours


class IdempotencyError(Exception):
    """Raised when idempotency validation fails."""
    pass


def compute_request_hash(request_body: bytes) -> str:
    """Compute SHA256 hash of request body for integrity verification."""
    return hashlib.sha256(request_body).hexdigest()


def get_cache_key(org_id: str, idempotency_key: str) -> str:
    """Generate cache key for idempotency lookup."""
    return f"{IDEMPOTENCY_CACHE_PREFIX}{org_id}:{idempotency_key}"


def get_idempotency_key(request) -> Optional[str]:
    """Extract Idempotency-Key header from request."""
    return request.headers.get("Idempotency-Key")


class IdempotencyManager:
    """
    Manage idempotent operations with replay protection.
    """

    @staticmethod
    def store_result(
        org_id: str,
        idempotency_key: str,
        operation_type: str,
        request_body: bytes,
        response_status: int,
        response_data: dict,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Store the result of an operation for replay protection.
        
        Args:
            org_id: Organization ID
            idempotency_key: Client-provided idempotency key
            operation_type: Type of operation (e.g., 'subscribe')
            request_body: Raw request body bytes
            response_status: HTTP status code
            response_data: Response JSON data
            error_message: Error message if operation failed
        """
        from apps.billing.models import IdempotencyKey

        request_hash = compute_request_hash(request_body)
        cache_key = get_cache_key(org_id, idempotency_key)

        # Store in cache (fast lookup)
        cache_data = {
            "status": response_status,
            "data": response_data,
            "error": error_message,
        }
        cache.set(cache_key, cache_data, timeout=IDEMPOTENCY_CACHE_TTL)

        # Store in database (permanent record)
        try:
            IdempotencyKey.objects.create(
                organization_id=org_id,
                idempotency_key=idempotency_key,
                operation_type=operation_type,
                request_hash=request_hash,
                response_status=response_status,
                response_data=response_data,
                error_message=error_message,
            )
        except Exception as exc:
            logger.error(f"Failed to store idempotency key: {exc}")

    @staticmethod
    def get_result(org_id: str, idempotency_key: str) -> Optional[dict]:
        """
        Retrieve cached result of a previous operation.
        
        Returns:
            Dict with 'status', 'data', 'error' if found, None otherwise.
        """
        cache_key = get_cache_key(org_id, idempotency_key)
        return cache.get(cache_key)

    @staticmethod
    def validate_request_integrity(
        org_id: str,
        idempotency_key: str,
        request_body: bytes,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate that a retry request matches the original (same body hash).
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        from apps.billing.models import IdempotencyKey

        request_hash = compute_request_hash(request_body)

        try:
            stored = IdempotencyKey.objects.get(
                organization_id=org_id,
                idempotency_key=idempotency_key,
            )
            if stored.request_hash != request_hash:
                msg = (
                    f"Request body mismatch for idempotency key {idempotency_key}. "
                    f"Possible replay attack or request modification."
                )
                logger.warning(msg)
                return False, msg
            return True, None
        except IdempotencyKey.DoesNotExist:
            # Key not found in DB yet (first attempt), not an error
            return True, None

    @staticmethod
    def cleanup_expired() -> int:
        """
        Remove idempotency keys older than 24 hours.
        
        Returns:
            Number of rows deleted
        """
        from apps.billing.models import IdempotencyKey

        cutoff = timezone.now() - timedelta(hours=24)
        count, _ = IdempotencyKey.objects.filter(created_at__lt=cutoff).delete()
        logger.info(f"Cleaned up {count} expired idempotency keys")
        return count


def ensure_idempotency(operation_type: str):
    """
    Decorator for API views to add idempotency support.
    
    Usage:
        @ensure_idempotency("subscribe")
        @api_view(["POST"])
        def subscribe(request):
            ...
    
    The decorator:
    1. Extracts Idempotency-Key header
    2. Checks for prior result in cache
    3. If found, returns cached response (replay protection)
    4. If not found, allows view to execute
    5. After view completes, stores result
    """
    def decorator(view_func):
        def wrapper(request, *args, **kwargs):
            org = getattr(request, "org", None)
            if not org:
                return view_func(request, *args, **kwargs)

            # Get idempotency key
            idempotency_key = get_idempotency_key(request)
            if not idempotency_key:
                # No idempotency key provided, allow request
                return view_func(request, *args, **kwargs)

            # Check for prior result
            cached_result = IdempotencyManager.get_result(
                str(org.id),
                idempotency_key,
            )
            if cached_result:
                logger.info(
                    f"Returning cached result for idempotent operation: "
                    f"{operation_type} (key={idempotency_key})"
                )
                return Response(
                    cached_result["data"],
                    status=cached_result["status"],
                )

            # Validate request integrity if we have a record
            request_body = request.body or b"{}"
            is_valid, error_msg = IdempotencyManager.validate_request_integrity(
                str(org.id),
                idempotency_key,
                request_body,
            )
            if not is_valid:
                return Response(
                    {"error": error_msg},
                    status=status.HTTP_409_CONFLICT,
                )

            # Execute the view
            try:
                response = view_func(request, *args, **kwargs)

                # Store result for replay protection
                response_data = response.data if hasattr(response, "data") else {}
                IdempotencyManager.store_result(
                    org_id=str(org.id),
                    idempotency_key=idempotency_key,
                    operation_type=operation_type,
                    request_body=request_body,
                    response_status=response.status_code,
                    response_data=response_data,
                    error_message=None,
                )

                return response

            except Exception as exc:
                logger.exception(f"Error in idempotent operation: {operation_type}")
                error_msg = str(exc)

                # Store error for replay protection
                IdempotencyManager.store_result(
                    org_id=str(org.id),
                    idempotency_key=idempotency_key,
                    operation_type=operation_type,
                    request_body=request_body,
                    response_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    response_data={"error": error_msg},
                    error_message=error_msg,
                )

                raise

        return wrapper
    return decorator
