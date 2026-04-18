"""
AuditLog API views.

Endpoints (all require OWNER or ADMIN role):
  GET  /audit-logs/          — paginated list with filters
  GET  /audit-logs/<id>/     — full event detail
  GET  /audit-logs/export/   — CSV download of filtered results
"""

import csv
import io
import logging

from django.utils.dateparse import parse_datetime

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.audit_logs.models import AuditLog
from apps.audit_logs.serializers import AuditLogSerializer
from apps.rbac.permissions import require_permission
from apps.rbac.registry import AUDIT_LOGS_READ

logger = logging.getLogger(__name__)


def _base_queryset(org):
    """Return AuditLog entries scoped to *org*, most recent first."""
    return AuditLog.objects.filter(org=org).select_related("actor", "org").order_by("-created_at")


def _apply_filters(qs, params):
    """Apply optional querystring filters to *qs*."""
    if actor := params.get("actor"):
        qs = qs.filter(actor__email__icontains=actor)
    if action := params.get("action"):
        qs = qs.filter(action__icontains=action)
    if resource_type := params.get("resource_type"):
        qs = qs.filter(resource_type__iexact=resource_type)
    if resource_id := params.get("resource_id"):
        qs = qs.filter(resource_id=resource_id)
    if since := params.get("since"):
        dt = parse_datetime(since)
        if dt:
            qs = qs.filter(created_at__gte=dt)
    if until := params.get("until"):
        dt = parse_datetime(until)
        if dt:
            qs = qs.filter(created_at__lte=dt)
    return qs


@api_view(["GET"])
@require_permission(AUDIT_LOGS_READ)
def list_audit_logs(request: Request) -> Response:
    """
    List audit log events for the caller's organisation.

    Query parameters (all optional):
      actor         — filter by actor email (case-insensitive substring)
      action        — filter by action string (substring)
      resource_type — filter by resource type (exact, case-insensitive)
      resource_id   — filter by resource PK string
      since         — ISO 8601 datetime lower bound (inclusive)
      until         — ISO 8601 datetime upper bound (inclusive)
      page          — page number (default 1)
      page_size     — results per page (default 20, max 200)
    """
    org = request.org
    qs = _apply_filters(_base_queryset(org), request.query_params)

    # Simple manual pagination (avoids DRF paginator boilerplate in FBVs)
    try:
        page_size = min(int(request.query_params.get("page_size", 20)), 200)
        page = max(int(request.query_params.get("page", 1)), 1)
    except ValueError:
        page_size, page = 20, 1

    total = qs.count()
    offset = (page - 1) * page_size
    entries = qs[offset : offset + page_size]

    serializer = AuditLogSerializer(entries, many=True)
    return Response(
        {
            "count": total,
            "page": page,
            "page_size": page_size,
            "total_pages": -(-total // page_size),  # ceiling division
            "results": serializer.data,
        }
    )


@api_view(["GET"])
@require_permission(AUDIT_LOGS_READ)
def get_audit_log(request: Request, log_id: str) -> Response:
    """
    Retrieve a single AuditLog entry by UUID.

    Returns 404 if the event does not belong to the caller's org.
    """
    try:
        entry = AuditLog.objects.select_related("actor", "org").get(
            id=log_id,
            org=request.org,
        )
    except AuditLog.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

    return Response(AuditLogSerializer(entry).data)


@api_view(["GET"])
@require_permission(AUDIT_LOGS_READ)
def export_audit_logs(request: Request) -> Response:
    """
    Export filtered audit logs as a downloadable CSV file.

    Accepts the same filter query parameters as list_audit_logs.
    Returns at most 10 000 rows to protect against huge downloads.
    """
    from django.http import HttpResponse

    org = request.org
    qs = _apply_filters(_base_queryset(org), request.query_params)[:10_000]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "created_at",
            "actor_email",
            "actor_id",
            "action",
            "resource_type",
            "resource_id",
            "ip_address",
            "user_agent",
            "request_id",
            "diff",
        ]
    )

    for entry in qs:
        writer.writerow(
            [
                str(entry.id),
                entry.created_at.isoformat(),
                entry.actor.email if entry.actor else "",
                str(entry.actor_id) if entry.actor_id else "",
                entry.action,
                entry.resource_type,
                entry.resource_id,
                entry.ip_address or "",
                entry.user_agent,
                entry.request_id,
                str(entry.diff),
            ]
        )

    response = HttpResponse(buf.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="audit-logs-{org.slug}.csv"'
    return response
