"""Health check view — GET /health."""

import django
from django.core.cache import cache
from django.db import connection

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """
    Returns HTTP 200 when all dependencies are healthy,
    HTTP 503 when any dependency is unavailable.
    """
    checks = {}

    # Database liveness
    try:
        connection.ensure_connection()
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"

    # Redis / cache liveness
    try:
        cache.set("__health__", "1", timeout=5)
        val = cache.get("__health__")
        checks["redis"] = "ok" if val == "1" else "unavailable"
    except Exception:
        checks["redis"] = "unavailable"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    http_status = status.HTTP_200_OK if overall == "ok" else status.HTTP_503_SERVICE_UNAVAILABLE

    return Response(
        {
            "status": overall,
            "checks": checks,
            "version": "1.0.0",
        },
        status=http_status,
    )
