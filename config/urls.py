"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path

from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.health import health_check
from apps.tenants.views import PublicInvitationView

urlpatterns = [
    path("admin/", admin.site.urls),
    # Health check
    path("health/", health_check, name="health-check"),
    # Auth routes
    path("auth/", include("apps.users.urls")),
    # Organisation / member-management
    path("orgs/", include("apps.tenants.urls")),
    # Global invitation token endpoints (unauthenticated GET, authenticated POST)
    path("invitations/<str:token>/", PublicInvitationView.as_view(), name="invitation-resolve"),
    path(
        "invitations/<str:token>/accept/", PublicInvitationView.as_view(), name="invitation-accept"
    ),
    # API Key management
    path("api-keys/", include("apps.api_keys.urls")),
    # Billing
    path("billing/", include("apps.billing.urls")),
    # Usage metering
    path("usage/", include("apps.usage.urls")),
    # Audit Logs
    path("audit-logs/", include("apps.audit_logs.urls")),
    # Feature Flags & Versioning
    path("", include("apps.features.urls")),
    # Core
    path("", include("apps.core.urls")),
    # OpenAPI schema
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
