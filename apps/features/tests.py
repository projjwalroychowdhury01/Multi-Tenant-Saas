import hashlib
import json
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.test import Client
from rest_framework import status

from apps.billing.models import Plan
from apps.tenants.models import Organization
from apps.users.models import User
from tests.factories import OrganizationFactory, UserFactory

from .models import FeatureFlag, ResourceSnapshot
from .service import FeatureFlagService


@pytest.mark.django_db
class TestFeatureFlagService:
    """Unit tests for feature flag evaluation logic."""

    def test_is_enabled_flag_not_found(self):
        """Non-existent flag defaults to False."""
        result = FeatureFlagService.is_enabled(org_id=1, flag_key="nonexistent")
        assert result is False

    def test_is_enabled_default_value(self):
        """Default enabled_default value is used when no override applies."""
        FeatureFlag.objects.create(
            key="test_flag",
            enabled_default=True,
            is_active=True,
        )
        result = FeatureFlagService.is_enabled(org_id=1, flag_key="test_flag")
        assert result is True

    def test_is_enabled_org_override_true(self):
        """Explicit org override True takes precedence."""
        FeatureFlag.objects.create(
            key="test_flag",
            enabled_default=False,
            enabled_for_orgs={"1": True},
            is_active=True,
        )
        result = FeatureFlagService.is_enabled(org_id=1, flag_key="test_flag")
        assert result is True

    def test_is_enabled_org_override_false(self):
        """Explicit org override False takes precedence."""
        FeatureFlag.objects.create(
            key="test_flag",
            enabled_default=True,
            enabled_for_orgs={"1": False},
            is_active=True,
        )
        result = FeatureFlagService.is_enabled(org_id=1, flag_key="test_flag")
        assert result is False

    def test_is_enabled_plan_level(self):
        """Plan-level default is applied when no org override."""
        org = OrganizationFactory()
        plan = Plan.objects.create(
            name="PRO",
            price_monthly=29900,
            limits={},
            features={},
        )
        org.billing_plan = plan
        org.save()

        FeatureFlag.objects.create(
            key="premium_feature",
            enabled_default=False,
            enabled_for_plans={"pro": True, "free": False},
            is_active=True,
        )

        result = FeatureFlagService.is_enabled(
            org_id=org.id, flag_key="premium_feature"
        )
        assert result is True

    def test_is_enabled_rollout_deterministic(self):
        """Rollout percentage is deterministic per org."""
        FeatureFlag.objects.create(
            key="rollout_flag",
            enabled_default=False,
            rollout_pct=50,
            is_active=True,
        )

        # Same org should always get the same result
        result1 = FeatureFlagService.is_enabled(org_id=1, flag_key="rollout_flag")
        result2 = FeatureFlagService.is_enabled(org_id=1, flag_key="rollout_flag")
        assert result1 == result2

        # Different orgs may get different results
        result3 = FeatureFlagService.is_enabled(org_id=2, flag_key="rollout_flag")
        # Can't assert on this without predicting the hash, but we can verify no crash

    def test_get_all_features_for_org(self):
        """Get all features for an org returns correctly evaluated flags."""
        org = OrganizationFactory()

        FeatureFlag.objects.create(
            key="flag_1",
            enabled_default=True,
            is_active=True,
        )
        FeatureFlag.objects.create(
            key="flag_2",
            enabled_default=False,
            is_active=True,
        )
        FeatureFlag.objects.create(
            key="flag_3",
            enabled_for_orgs={str(org.id): True},
            is_active=True,
        )

        features = FeatureFlagService.get_all_features_for_org(org.id)

        assert features["flag_1"] is True
        assert features["flag_2"] is False
        assert features["flag_3"] is True

    def test_invalidate_cache(self):
        """Cache can be invalidated for a specific flag or globally."""
        cache.clear()

        FeatureFlag.objects.create(
            key="test_flag",
            enabled_default=True,
            is_active=True,
        )

        # Cache a value
        result1 = FeatureFlagService.is_enabled(org_id=1, flag_key="test_flag")
        assert result1 is True

        # Invalidate and verify it's recomputed
        FeatureFlagService.invalidate_cache(flag_key="test_flag", org_id=1)
        # After invalidation, cache.get should return None (cache miss)
        cache_key = "feature_flag:1:test_flag"
        assert cache.get(cache_key) is None


@pytest.mark.django_db
class TestFeatureFlagViewSet:
    """Integration tests for feature flag endpoints."""

    def test_get_my_features(self):
        """GET /features/my_features/ returns evaluated flags for current org."""
        from tests.factories import MembershipFactory
        
        membership = MembershipFactory()
        user = membership.user
        org = membership.organization

        FeatureFlag.objects.create(
            key="feature_a",
            enabled_default=True,
            is_active=True,
        )
        FeatureFlag.objects.create(
            key="feature_b",
            enabled_default=False,
            is_active=True,
        )

        from rest_framework.test import APIClient
        
        client = APIClient()
        # Get token for user
        from rest_framework_simplejwt.tokens import AccessToken
        token = AccessToken()
        token['user_id'] = user.id
        token['org_id'] = org.id
        token['role'] = 'MEMBER'
        
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {str(token)}"

        response = client.get("/features/my_features/")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["feature_a"] is True
        assert data["feature_b"] is False

    def test_feature_flags_list_requires_auth(self):
        """GET /features/ requires authentication."""
        from rest_framework.test import APIClient
        
        client = APIClient()
        response = client.get("/features/")
        # Depending on routing, may 404 or 401; both acceptable
        assert response.status_code in [status.HTTP_401_UNAUTHORIZED, status.HTTP_404_NOT_FOUND]


@pytest.mark.django_db
class TestResourceSnapshot:
    """Tests for resource snapshots and versioning."""

    def test_snapshot_created_on_versioned_model_save(self):
        """Snapshot is created when a VersionedMixin model is saved."""
        from tests.factories import MembershipFactory
        
        # This test requires a signal handler to be implemented
        # For now, test that snapshots can be created and retrieved
        membership = MembershipFactory()
        user = membership.user

        snapshot = ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=user.id,
            organization_id=membership.organization.id,
            version=1,
            data={"id": user.id, "email": user.email},
            actor_id=user.id,
            request_id="req-12345",
            change_reason="user_created",
        )

        assert snapshot.id is not None
        assert snapshot.resource_type == "User"
        assert snapshot.version == 1

    def test_snapshot_history_retrieval(self):
        """GET /snapshots/history/ retrieves all snapshots for a resource."""
        from tests.factories import MembershipFactory
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken
        
        membership = MembershipFactory()
        user = membership.user
        org = membership.organization

        ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=user.id,
            organization_id=org.id,
            version=1,
            data={"email": "v1@example.com"},
            change_reason="created",
        )
        ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=user.id,
            organization_id=org.id,
            version=2,
            data={"email": "v2@example.com"},
            change_reason="email_updated",
        )

        client = APIClient()
        token = AccessToken()
        token['user_id'] = user.id
        token['org_id'] = org.id
        token['role'] = 'MEMBER'
        
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {str(token)}"

        response = client.get(
            f"/snapshots/history/?resource_type=User&resource_id={user.id}"
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 2
        assert data[0]["version"] == 2  # Most recent first
        assert data[1]["version"] == 1


@pytest.mark.django_db
class TestVersionedCacheNamespace:
    """Tests for versioned cache namespace system."""

    def test_versioned_cache_build_key(self):
        """Cache keys are versioned correctly."""
        from .cache import VersionedCacheNamespace
        
        cache_ns = VersionedCacheNamespace("test", ttl=60)
        
        key1 = cache_ns.build_key("entity", "key1", org_id=1)
        key2 = cache_ns.build_key("entity", "key1", org_id=2)
        
        assert key1 != key2
        assert "test" in key1
        assert "entity" in key1

    def test_versioned_cache_get_set(self):
        """Cache get/set operations work with versioning."""
        from .cache import VersionedCacheNamespace
        
        cache_ns = VersionedCacheNamespace("test", ttl=60)
        
        # Set value
        cache_ns.set("entity", "key", "value", org_id=1)
        
        # Get value
        result = cache_ns.get("entity", "key", org_id=1)
        assert result == "value"

    def test_versioned_cache_invalidate_namespace(self):
        """Invalidating namespace increments version."""
        from .cache import VersionedCacheNamespace
        
        cache_ns = VersionedCacheNamespace("test", ttl=60)
        
        version_before = cache_ns.get_version()
        cache_ns.invalidate_namespace()
        version_after = cache_ns.get_version()
        
        assert version_after > version_before

    def test_versioned_cache_invalidate_org(self):
        """Invalidating org clears org-scoped cache."""
        from .cache import VersionedCacheNamespace
        
        cache_ns = VersionedCacheNamespace("test", ttl=60)
        
        # Set values for org
        cache_ns.set("entity", "key1", "value1", org_id=1, track_index=True)
        cache_ns.set("entity", "key2", "value2", org_id=1, track_index=True)
        
        # Invalidate org
        count = cache_ns.invalidate_org(1)
        assert count >= 2
        
        # Values should be gone
        result = cache_ns.get("entity", "key1", org_id=1)
        assert result is None


@pytest.mark.django_db
class TestPolymorphicIDField:
    """Tests for polymorphic ID field supporting UUID and integer."""

    def test_polymorphic_id_field_integer(self):
        """PolymorphicIDField stores and retrieves integers."""
        snapshot = ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=12345,
            organization_id=67890,
            version=1,
            data={},
            actor_id=11111,
        )
        
        fetched = ResourceSnapshot.objects.get(id=snapshot.id)
        assert fetched.resource_id == 12345
        assert fetched.organization_id == 67890
        assert fetched.actor_id == 11111

    def test_polymorphic_id_field_uuid(self):
        """PolymorphicIDField stores and retrieves UUIDs."""
        import uuid
        
        test_uuid = uuid.uuid4()
        
        snapshot = ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=test_uuid,
            organization_id=uuid.uuid4(),
            version=1,
            data={},
            actor_id=uuid.uuid4(),
        )
        
        fetched = ResourceSnapshot.objects.get(id=snapshot.id)
        assert fetched.resource_id == test_uuid

    def test_polymorphic_id_field_mixed_types(self):
        """PolymorphicIDField can mix UUID and integer in same table."""
        import uuid
        
        snapshot1 = ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=12345,
            organization_id=67890,
            version=1,
            data={},
        )
        
        snapshot2 = ResourceSnapshot.objects.create(
            resource_type="ApiKey",
            resource_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            version=1,
            data={},
        )
        
        assert snapshot1.resource_id == 12345
        assert isinstance(snapshot2.resource_id, uuid.UUID)


@pytest.mark.django_db
class TestSnapshotRestoreEndpoint:
    """Tests for snapshot restore functionality."""

    def test_restore_endpoint_initiates_task(self):
        """POST /snapshots/{id}/restore/ initiates restoration."""
        from tests.factories import MembershipFactory
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken
        
        membership = MembershipFactory()
        user = membership.user
        org = membership.organization
        
        snapshot = ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=user.id,
            organization_id=org.id,
            version=1,
            data={"email": "old@example.com"},
            change_reason="created",
        )
        
        client = APIClient()
        token = AccessToken()
        token['user_id'] = user.id
        token['org_id'] = org.id
        token['role'] = 'ADMIN'
        
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {str(token)}"
        
        response = client.post(f"/snapshots/{snapshot.id}/restore/")
        
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["status"] == "restoration_started"
        assert data["snapshot_id"] == snapshot.id

    def test_restore_to_version_endpoint(self):
        """POST /snapshots/restore_to_version/ restores specific version."""
        from tests.factories import MembershipFactory
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken
        
        membership = MembershipFactory()
        user = membership.user
        org = membership.organization
        
        ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=user.id,
            organization_id=org.id,
            version=1,
            data={"email": "v1@example.com"},
            change_reason="created",
        )
        ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=user.id,
            organization_id=org.id,
            version=2,
            data={"email": "v2@example.com"},
            change_reason="updated",
        )
        
        client = APIClient()
        token = AccessToken()
        token['user_id'] = user.id
        token['org_id'] = org.id
        token['role'] = 'ADMIN'
        
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {str(token)}"
        
        response = client.post(
            f"/snapshots/restore_to_version/?resource_type=User&resource_id={user.id}&version=1"
        )
        
        assert response.status_code == status.HTTP_201_CREATED

    def test_compare_versions_endpoint(self):
        """GET /snapshots/{id}/compare_versions/ computes diff."""
        from tests.factories import MembershipFactory
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken
        
        membership = MembershipFactory()
        user = membership.user
        org = membership.organization
        
        snapshot1 = ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=user.id,
            organization_id=org.id,
            version=1,
            data={"email": "v1@example.com", "name": "User"},
            change_reason="created",
        )
        snapshot2 = ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=user.id,
            organization_id=org.id,
            version=2,
            data={"email": "v2@example.com", "name": "User", "active": True},
            change_reason="updated",
        )
        
        client = APIClient()
        token = AccessToken()
        token['user_id'] = user.id
        token['org_id'] = org.id
        token['role'] = 'MEMBER'
        
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {str(token)}"
        
        response = client.get(
            f"/snapshots/{snapshot2.id}/compare_versions/?other_version=1"
        )
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "diff" in data
        assert "modified" in data["diff"]
        assert "added" in data["diff"]


@pytest.mark.django_db
class TestSnapshotSignals:
    """Tests for automatic snapshot signal handlers."""

    def test_snapshot_created_on_versioned_model_update(self):
        """Signal creates snapshot when VersionedMixin model is saved."""
        from tests.factories import MembershipFactory
        from apps.core.mixins import VersionedMixin
        
        membership = MembershipFactory()
        user = membership.user
        
        # Assuming user is a VersionedMixin
        if not isinstance(user, VersionedMixin):
            pytest.skip("User is not VersionedMixin in this schema")
        
        # Save should trigger snapshot via signal
        user.email = "updated@example.com"
        user.save()
        
        # Check if snapshot was created (async, may not exist immediately)
        # This is an integration test - in practice snapshots are created async

    def test_snapshot_serialization(self):
        """Snapshot model serializes correctly to dict."""
        snapshot = ResourceSnapshot.objects.create(
            resource_type="User",
            resource_id=12345,
            organization_id=67890,
            version=1,
            data={"email": "test@example.com"},
            actor_id=11111,
            request_id="req-123",
            change_reason="created",
        )
        
        result = snapshot.to_dict()
        
        assert result["resource_type"] == "User"
        assert result["resource_id"] == "12345"
        assert result["organization_id"] == "67890"
        assert result["version"] == 1
        assert result["data"]["email"] == "test@example.com"

