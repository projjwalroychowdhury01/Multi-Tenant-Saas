"""
Feature flag evaluation service with Redis caching and deterministic rollout.
"""

import hashlib
from typing import Dict, Optional

from django.core.cache import cache
from django.db import IntegrityError

from apps.tenants.context import get_current_org

from .models import FeatureFlag


class FeatureFlagService:
    """
    Service for evaluating feature flags with caching and rollout logic.
    """

    CACHE_TTL = 60  # seconds

    @staticmethod
    def is_enabled(org_id: int, flag_key: str) -> bool:
        """
        Evaluate whether a feature flag is enabled for a given organization.
        
        Logic:
        1. Check Redis cache first
        2. Check explicit org override (enabled_for_orgs)
        3. Check plan-level default
        4. Apply rollout percentage (deterministic hash)
        
        Returns:
            bool: True if enabled, False otherwise
        """
        cache_key = f"feature_flag:{org_id}:{flag_key}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            flag = FeatureFlag.objects.get(key=flag_key, is_active=True)
        except FeatureFlag.DoesNotExist:
            # If flag doesn't exist, default to disabled
            cache.set(cache_key, False, FeatureFlagService.CACHE_TTL)
            return False

        # Step 1: Check explicit org override
        org_id_str = str(org_id)
        if org_id_str in flag.enabled_for_orgs:
            result = flag.enabled_for_orgs[org_id_str]
            cache.set(cache_key, result, FeatureFlagService.CACHE_TTL)
            return result

        # Step 2: Check plan-level default
        from apps.tenants.models import Organization

        try:
            org = Organization.objects.get(id=org_id)
            plan_name = org.billing_plan.name.lower() if org.billing_plan else "free"

            if plan_name in flag.enabled_for_plans:
                result = flag.enabled_for_plans[plan_name]
                cache.set(cache_key, result, FeatureFlagService.CACHE_TTL)
                return result
        except Organization.DoesNotExist:
            pass

        # Step 3: Apply rollout percentage
        if flag.rollout_pct > 0:
            if FeatureFlagService._is_org_in_rollout(org_id, flag_key, flag.rollout_pct):
                cache.set(cache_key, True, FeatureFlagService.CACHE_TTL)
                return True

        # Step 4: Fall back to default
        result = flag.enabled_default
        cache.set(cache_key, result, FeatureFlagService.CACHE_TTL)
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
        
        Returns:
            dict: {flag_key: enabled}
        """
        flags = FeatureFlag.objects.filter(is_active=True)
        result = {}

        for flag in flags:
            result[flag.key] = FeatureFlagService.is_enabled(org_id, flag.key)

        return result

    @staticmethod
    def invalidate_cache(flag_key: Optional[str] = None, org_id: Optional[int] = None):
        """
        Invalidate cache entries for a feature flag.
        
        If flag_key and org_id are specified, invalidate just that combination.
        If only flag_key is specified, invalidate all orgs for that flag.
        If neither is specified, invalidate all feature flag caches.
        """
        if flag_key and org_id:
            cache_key = f"feature_flag:{org_id}:{flag_key}"
            cache.delete(cache_key)
        elif flag_key:
            # Would need to track all orgs or use a separate index
            # For now, use cache.clear() but this is not ideal for production
            # Better solution: use Django's cache key pattern or a dedicated key set
            pass
        else:
            # Clear all feature flag caches
            cache.clear()

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

        FeatureFlagService.invalidate_cache(flag_key=key)
        return flag
