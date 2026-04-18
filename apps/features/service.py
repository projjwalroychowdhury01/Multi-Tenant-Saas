"""
Feature flag evaluation service with Redis caching and deterministic rollout.
"""

import hashlib
from typing import Dict, Optional

from django.core.cache import cache
from django.db import IntegrityError

from apps.tenants.context import get_current_org

from .cache import VersionedCacheNamespace, CacheKeyBuilder
from .models import FeatureFlag


# Initialize versioned cache namespace for feature flags
_feature_cache = VersionedCacheNamespace("feature", ttl=60)


class FeatureFlagService:
    """
    Service for evaluating feature flags with versioned caching and rollout logic.
    """

    CACHE_TTL = 60  # seconds

    @staticmethod
    def is_enabled(org_id: int, flag_key: str) -> bool:
        """
        Evaluate whether a feature flag is enabled for a given organization.

        Logic:
        1. Check versioned Redis cache first
        2. Check explicit org override (enabled_for_orgs)
        3. Check plan-level default
        4. Apply rollout percentage (deterministic hash)

        Returns:
            bool: True if enabled, False otherwise
        """
        # Try versioned cache first
        cache_key = CacheKeyBuilder.feature_flag_key(org_id, flag_key)
        cached = _feature_cache.get("flag", cache_key, org_id=org_id)
        if cached is not None:
            return cached

        try:
            flag = FeatureFlag.objects.get(key=flag_key, is_active=True)
        except FeatureFlag.DoesNotExist:
            # If flag doesn't exist, default to disabled
            _feature_cache.set("flag", cache_key, False, org_id=org_id, track_index=True)
            return False

        # Step 1: Check explicit org override
        org_id_str = str(org_id)
        if org_id_str in flag.enabled_for_orgs:
            result = flag.enabled_for_orgs[org_id_str]
            _feature_cache.set("flag", cache_key, result, org_id=org_id, track_index=True)
            return result

        # Step 2: Check plan-level default
        from apps.tenants.models import Organization

        try:
            org = Organization.objects.get(id=org_id)
            plan_name = org.billing_plan.name.lower() if org.billing_plan else "free"

            if plan_name in flag.enabled_for_plans:
                result = flag.enabled_for_plans[plan_name]
                _feature_cache.set("flag", cache_key, result, org_id=org_id, track_index=True)
                return result
        except Organization.DoesNotExist:
            pass

        # Step 3: Apply rollout percentage
        if flag.rollout_pct > 0:
            if FeatureFlagService._is_org_in_rollout(org_id, flag_key, flag.rollout_pct):
                _feature_cache.set("flag", cache_key, True, org_id=org_id, track_index=True)
                return True

        # Step 4: Fall back to default
        result = flag.enabled_default
        _feature_cache.set("flag", cache_key, result, org_id=org_id, track_index=True)
        return result

    @staticmethod
    def _is_org_in_rollout(org_id: int, flag_key: str, rollout_pct: int) -> bool:
        """
        Deterministic check if org is in the rollout bucket.
        Uses consistent hashing so the same org always gets the same result.
        """
        if rollout_pct <= 0:
            return False
        if rollout_pct >= 100:
            return True

        # Hash org_id + flag_key to get a consistent bucket assignment
        hash_input = f"{org_id}:{flag_key}".encode()
        hash_value = int(hashlib.md5(hash_input).hexdigest(), 16)
        bucket = hash_value % 100

        return bucket < rollout_pct

    @staticmethod
    def get_all_features_for_org(org_id: int) -> Dict[str, bool]:
        """
        Evaluate all active feature flags for a given organization.

        Uses versioned cache for bulk operations.

        Returns:
            dict: {flag_key: enabled}
        """
        # Try to get from cache first
        cache_key = CacheKeyBuilder.org_features_key(org_id)
        cached = _feature_cache.get("features", cache_key, org_id=org_id)
        if cached:
            return cached

        flags = FeatureFlag.objects.filter(is_active=True)
        result = {}

        for flag in flags:
            result[flag.key] = FeatureFlagService.is_enabled(org_id, flag.key)

        # Store in cache
        _feature_cache.set("features", cache_key, result, org_id=org_id, track_index=True)

        return result

    @staticmethod
    def invalidate_cache(flag_key: Optional[str] = None, org_id: Optional[int] = None) -> int:
        """
        Invalidate cache entries for a feature flag using versioned namespacing.

        Args:
            flag_key: Specific flag to invalidate
            org_id: Specific org to invalidate

        Returns:
            Number of entries invalidated
        """
        count = 0

        if flag_key and org_id:
            # Invalidate specific flag for specific org
            cache_key = CacheKeyBuilder.feature_flag_key(org_id, flag_key)
            _feature_cache.delete("flag", cache_key, org_id=org_id)
            count += 1
        elif flag_key:
            # Invalidate flag for all orgs
            count += _feature_cache.invalidate_entity(f"flag:{flag_key}")
        elif org_id:
            # Invalidate all flags for specific org
            count += _feature_cache.invalidate_org(org_id)
        else:
            # Invalidate entire namespace
            _feature_cache.invalidate_namespace()
            count = 1

        return count

    @staticmethod
    def create_or_update_flag(
        key: str,
        description: str = "",
        enabled_default: bool = False,
        enabled_for_plans: dict = None,
        enabled_for_orgs: dict = None,
        rollout_pct: int = 0,
        metadata: dict = None,
        is_active: bool = True,
    ) -> FeatureFlag:
        """
        Create or update a feature flag.
        """
        enabled_for_plans = enabled_for_plans or {}
        enabled_for_orgs = enabled_for_orgs or {}
        metadata = metadata or {}

        flag, created = FeatureFlag.objects.update_or_create(
            key=key,
            defaults={
                "description": description,
                "enabled_default": enabled_default,
                "enabled_for_plans": enabled_for_plans,
                "enabled_for_orgs": enabled_for_orgs,
                "rollout_pct": rollout_pct,
                "metadata": metadata,
                "is_active": is_active,
            },
        )

        # Invalidate cache for this flag across all orgs
        FeatureFlagService.invalidate_cache(flag_key=key)
        return flag

    @staticmethod
    def get_cache_stats() -> dict:
        """Get cache namespace statistics."""
        return {
            "namespace": _feature_cache.namespace,
            "version": _feature_cache.get_version(),
            "ttl": _feature_cache.ttl,
        }
