"""
Health check endpoint for monitoring DB and Redis connectivity.
"""

from django.core.cache import cache
from django.db import connection
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """
    GET /health
    
    Returns the health status of critical services:
    - Database connectivity
    - Redis/Cache connectivity
    """
    health = {
        "status": "healthy",
        "services": {},
    }

    # Check database
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        health["services"]["database"] = {"status": "ok"}
    except Exception as e:
        health["services"]["database"] = {"status": "error", "error": str(e)}
        health["status"] = "unhealthy"

    # Check Redis/Cache
    try:
        cache.set("health_check", "ok", 10)
        if cache.get("health_check") == "ok":
            health["services"]["cache"] = {"status": "ok"}
        else:
            raise Exception("Cache get/set mismatch")
    except Exception as e:
        health["services"]["cache"] = {"status": "error", "error": str(e)}
        health["status"] = "unhealthy"

    status_code = HTTP_200_OK if health["status"] == "healthy" else HTTP_503_SERVICE_UNAVAILABLE

    return Response(health, status=status_code)
