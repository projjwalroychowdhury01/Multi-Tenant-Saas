from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
)

from apps.rbac.permissions import HasTenantPermission

from .models import FeatureFlag, ResourceSnapshot
from .serializers import (
    FeatureFlagEvaluationSerializer,
    FeatureFlagSerializer,
    ResourceSnapshotSerializer,
)
from .service import FeatureFlagService
from .tasks import restore_resource_snapshot


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

    Provides:
    - List and retrieve snapshots
    - Full version history retrieval
    - Restore from snapshot functionality
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
                status=HTTP_400_BAD_REQUEST,
            )

        snapshots = ResourceSnapshot.objects.filter(
            resource_type=resource_type,
            resource_id=resource_id,
        ).order_by("-version")

        serializer = self.get_serializer(snapshots, many=True)
        return Response(serializer.data, status=HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        """
        POST /snapshots/{id}/restore/

        Restore a resource from a snapshot. This will apply the snapshot data
        back to the resource and create a new snapshot recording the restoration.

        Query parameters:
        - apply_changes (bool, default=true): Whether to apply snapshot data as update

        Returns:
            Restoration status and details
        """
        snapshot = self.get_object()
        apply_changes = request.query_params.get("apply_changes", "true").lower() == "true"

        try:
            # Check permissions - must be able to modify the resource
            if not request.user.is_authenticated:
                return Response(
                    {"error": "Authentication required"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # Enqueue async restoration task
            task = restore_resource_snapshot.delay(
                snapshot_id=snapshot.id,
                restore_changes=apply_changes,
            )

            return Response(
                {
                    "status": "restoration_started",
                    "snapshot_id": snapshot.id,
                    "resource_type": snapshot.resource_type,
                    "resource_id": str(snapshot.resource_id),
                    "version": snapshot.version,
                    "task_id": task.id,
                    "message": f"Restoring {snapshot.resource_type}#{snapshot.resource_id} from v{snapshot.version}",
                },
                status=HTTP_201_CREATED,
            )

        except Exception as exc:
            return Response(
                {
                    "error": str(exc),
                    "snapshot_id": snapshot.id,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=False, methods=["post"])
    def restore_to_version(self, request):
        """
        POST /snapshots/restore_to_version/

        Restore a resource to a specific version by query parameters.

        Query parameters:
        - resource_type (required): Model class name
        - resource_id (required): Resource primary key
        - version (required): Version number to restore to

        Returns:
            Restoration status
        """
        resource_type = request.query_params.get("resource_type")
        resource_id = request.query_params.get("resource_id")
        version = request.query_params.get("version")

        if not all([resource_type, resource_id, version]):
            return Response(
                {"error": "resource_type, resource_id, and version are required"},
                status=HTTP_400_BAD_REQUEST,
            )

        try:
            version = int(version)
            snapshot = ResourceSnapshot.objects.get(
                resource_type=resource_type,
                resource_id=resource_id,
                version=version,
            )
        except (ValueError, ResourceSnapshot.DoesNotExist):
            return Response(
                {"error": f"Snapshot not found for {resource_type}#{resource_id} v{version}"},
                status=HTTP_404_NOT_FOUND,
            )

        # Delegate to restore endpoint
        request.parser_context["kwargs"]["pk"] = snapshot.id
        return self.restore(request, pk=snapshot.id)

    @action(detail=True, methods=["get"])
    def compare_versions(self, request, pk=None):
        """
        GET /snapshots/{id}/compare_versions/?other_version=5

        Compare current snapshot with another version.

        Query parameters:
        - other_version: Version number to compare against

        Returns:
            Diff showing what changed between versions
        """
        snapshot = self.get_object()
        other_version = request.query_params.get("other_version")

        if not other_version:
            return Response(
                {"error": "other_version parameter required"},
                status=HTTP_400_BAD_REQUEST,
            )

        try:
            other_version = int(other_version)
            other_snapshot = ResourceSnapshot.objects.get(
                resource_type=snapshot.resource_type,
                resource_id=snapshot.resource_id,
                version=other_version,
            )
        except (ValueError, ResourceSnapshot.DoesNotExist):
            return Response(
                {"error": f"Snapshot v{other_version} not found"},
                status=HTTP_404_NOT_FOUND,
            )

        # Compute diff
        diff = self._compute_diff(snapshot.data, other_snapshot.data)

        return Response(
            {
                "from_version": other_snapshot.version,
                "to_version": snapshot.version,
                "diff": diff,
                "from_snapshot_id": other_snapshot.id,
                "to_snapshot_id": snapshot.id,
            },
            status=HTTP_200_OK,
        )

    @staticmethod
    def _compute_diff(data1: dict, data2: dict) -> dict:
        """
        Compute a diff between two snapshot data dictionaries.

        Returns:
            Dict with 'added', 'removed', 'modified' keys
        """
        diff = {
            "added": {},
            "removed": {},
            "modified": {},
        }

        # Find removed and modified
        for key, value1 in data1.items():
            if key not in data2:
                diff["removed"][key] = value1
            elif data2[key] != value1:
                diff["modified"][key] = {"old": value1, "new": data2[key]}

        # Find added
        for key, value2 in data2.items():
            if key not in data1:
                diff["added"][key] = value2

        return diff
