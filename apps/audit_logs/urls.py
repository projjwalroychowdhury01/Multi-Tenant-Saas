"""URL patterns for the audit_logs app — mounted at /audit-logs/ in config/urls.py."""

from django.urls import path

from apps.audit_logs.views import (
    list_audit_logs,
    get_audit_log,
    export_audit_logs,
)

app_name = "audit_logs"

urlpatterns = [
    path("", list_audit_logs, name="list"),
    path("export/", export_audit_logs, name="export"),
    path("<uuid:log_id>/", get_audit_log, name="detail"),
]
