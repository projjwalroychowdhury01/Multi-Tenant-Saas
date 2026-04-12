"""Root URL configuration."""

from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
    # Auth routes
    path("auth/", include("apps.users.urls")),
    # Organisation / member-management
    path("orgs/", include("apps.tenants.urls")),
    # Core
    path("", include("apps.core.urls")),
    # OpenAPI schema
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
