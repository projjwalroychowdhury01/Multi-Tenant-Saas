from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK

from apps.rbac.permissions import HasTenantPermission

from .models import FeatureFlag, ResourceSnapshot
from .serializers import (
    FeatureFlagEvaluationSerializer,
    FeatureFlagSerializer,
    ResourceSnapshotSerializer,
)
from .service import FeatureFlagService


class FeatureFlagViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing feature flags.
    Only ADMIN+ can create/update/delete flags.
    VIEWER+ can read.
    All authenticated users can access `/me/features`.
    """

    queryset = FeatureFlag.objects.filter(is_active=True)
    serializer_class = FeatureFlagSerializer
    permission_classes = [IsAuthenticated, HasTenantPermission]

    def check_permissions(self, request):
        """
        Override to allow GET /me/features for all authenticated users.
        """
        if self.action == "my_features":
            return
        super().check_permissions(request)

    @action(detail=False, methods=["get"], permission_classes=[IsAuthenticated])
    def my_features(self, request):
        """
        GET /features/my_features/
        
        Returns all feature flags evaluated for the current organization.
        Format: {"feature_key": true/false, ...}
        """
        org = request.org
        features = FeatureFlagService.get_all_features_for_org(org.id)

        serializer = FeatureFlagEvaluationSerializer(features)
        return Response(serializer.data, status=HTTP_200_OK)


class ResourceSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing resource snapshots and version history.
    """

    queryset = ResourceSnapshot.objects.all()
    serializer_class = ResourceSnapshotSerializer
    permission_classes = [IsAuthenticated, HasTenantPermission]
    filterset_fields = ["resource_type", "resource_id", "organization_id"]
    ordering_fields = ["created_at", "version"]
    ordering = ["-created_at"]

    @action(detail=False, methods=["get"])
    def history(self, request):
        """
        GET /snapshots/history/?resource_type=User&resource_id=123
        
        Get full version history for a specific resource.
        """
        resource_type = request.query_params.get("resource_type")
        resource_id = request.query_params.get("resource_id")

        if not resource_type or not resource_id:
            return Response(
                {"error": "resource_type and resource_id are required"},
                status=400,
            )

        snapshots = ResourceSnapshot.objects.filter(
            resource_type=resource_type,
            resource_id=resource_id,
        ).order_by("-version")

        serializer = self.get_serializer(snapshots, many=True)
        return Response(serializer.data, status=HTTP_200_OK)
