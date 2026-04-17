from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import FeatureFlagViewSet, ResourceSnapshotViewSet

router = DefaultRouter()
router.register(r"features", FeatureFlagViewSet, basename="feature-flag")
router.register(r"snapshots", ResourceSnapshotViewSet, basename="resource-snapshot")

urlpatterns = [
    path("", include(router.urls)),
]
