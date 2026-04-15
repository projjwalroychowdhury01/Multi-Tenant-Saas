"""
AuditLog app configuration.
"""

from django.apps import AppConfig


class AuditLogsConfig(AppConfig):
    name = "apps.audit_logs"
    label = "audit_logs"
    verbose_name = "Audit Logs"

    def ready(self):
        # Import signals so Django registers them at startup
        import apps.audit_logs.signals  # noqa: F401
