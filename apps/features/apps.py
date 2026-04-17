from django.apps import AppConfig


class FeaturesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.features"
    verbose_name = "Feature Flags"

    def ready(self):
        """Register signal handlers when app is ready."""
        # Import signal handlers to register them
        from . import signals  # noqa: F401
