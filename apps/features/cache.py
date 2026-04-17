"""
Robust versioned cache namespace system for feature invalidation.

Supports:
  - Versioned cache key patterns with namespace isolation
  - Fine-grained cache invalidation at multiple levels
  - Index tracking for efficient bulk invalidation
  - Multi-tenant cache scoping
"""

import hashlib
from typing import Any, Iterable, Optional, Union

from django.core.cache import cache


class VersionedCacheNamespace:
    """
    Versioned cache namespace manager for robust cache invalidation.
    
    Provides:
    - Hierarchical cache key patterns: ns:version:entity:scope:key
    - Version-based invalidation (invalidate all keys for a version)
    - Index tracking for efficient bulk operations
    - Multi-level scoping (global, tenant, resource, entity-type)
    """

    # Cache keys for tracking versions and indices
    NAMESPACE_VERSION_KEY = "cache:ns:{namespace}:version"
    INDEX_KEY = "cache:ns:{namespace}:index:{level}"
    
    def __init__(self, namespace: str = "feature", ttl: int = 3600):
        """
        Initialize a versioned cache namespace.
        
        Args:
            namespace: Namespace identifier (e.g., 'feature', 'snapshot')
            ttl: Default time-to-live for cache entries (seconds)
        """
        self.namespace = namespace
        self.ttl = ttl
        self._ensure_version()

    def _ensure_version(self) -> int:
        """Ensure namespace version exists, initialize if needed."""
        version_key = self.NAMESPACE_VERSION_KEY.format(namespace=self.namespace)
        version = cache.get(version_key)
        
        if version is None:
            version = 1
            cache.set(version_key, version, timeout=None)  # No expiration for version
        
        return version

    def get_version(self) -> int:
        """Get current namespace version."""
        version_key = self.NAMESPACE_VERSION_KEY.format(namespace=self.namespace)
        return cache.get(version_key, 1)

    def build_key(
        self,
        entity: str,
        key: str,
        org_id: Optional[Union[str, int]] = None,
        resource_id: Optional[Union[str, int]] = None,
        version: Optional[int] = None,
    ) -> str:
        """
        Build a versioned cache key.
        
        Format: cache:ns:{namespace}:{version}:{entity}:{scope}:{key}
        
        Args:
            entity: Entity type (e.g., 'flag', 'snapshot')
            key: Cache key identifier
            org_id: Tenant identifier for scope
            resource_id: Resource identifier for scope
            version: Explicit version (auto-fetched if None)
        
        Returns:
            Fully qualified versioned cache key
        """
        if version is None:
            version = self.get_version()
        
        parts = [f"cache:ns:{self.namespace}:{version}:{entity}"]
        
        # Build scope from org_id and resource_id
        if org_id is not None:
            parts.append(f"org:{org_id}")
        
        if resource_id is not None:
            parts.append(f"res:{resource_id}")
        
        parts.append(key)
        
        return ":".join(parts)

    def get(self, entity: str, key: str, **scope) -> Optional[Any]:
        """Get value from versioned cache."""
        cache_key = self.build_key(entity, key, **scope)
        return cache.get(cache_key)

    def set(
        self,
        entity: str,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        track_index: bool = True,
        **scope
    ) -> str:
        """
        Set value in versioned cache.
        
        Args:
            entity: Entity type
            key: Cache key identifier
            value: Value to cache
            ttl: Override default TTL
            track_index: Whether to track this key for bulk invalidation
            **scope: Scope parameters (org_id, resource_id)
        
        Returns:
            Full cache key
        """
        cache_key = self.build_key(entity, key, **scope)
        cache.set(cache_key, value, timeout=ttl or self.ttl)
        
        if track_index:
            self._track_key(entity, cache_key, **scope)
        
        return cache_key

    def delete(self, entity: str, key: str, **scope) -> None:
        """Delete value from versioned cache."""
        cache_key = self.build_key(entity, key, **scope)
        cache.delete(cache_key)

    def _track_key(
        self,
        entity: str,
        cache_key: str,
        org_id: Optional[Union[str, int]] = None,
        resource_id: Optional[Union[str, int]] = None,
    ) -> None:
        """Track cache key in index for bulk invalidation."""
        # Track at entity level
        entity_index = self.INDEX_KEY.format(
            namespace=self.namespace, level=f"entity:{entity}"
        )
        cache.set(entity_index, {cache_key}, timeout=None)
        
        # Track at org level if scoped
        if org_id is not None:
            org_index = self.INDEX_KEY.format(
                namespace=self.namespace, level=f"org:{org_id}"
            )
            existing = cache.get(org_index, set())
            existing.add(cache_key)
            cache.set(org_index, existing, timeout=None)

    def invalidate_entity(self, entity: str) -> int:
        """
        Invalidate all cache entries for an entity type.
        
        Returns:
            Number of keys invalidated
        """
        entity_index = self.INDEX_KEY.format(
            namespace=self.namespace, level=f"entity:{entity}"
        )
        keys = cache.get(entity_index, set())
        
        count = 0
        for key in keys:
            cache.delete(key)
            count += 1
        
        cache.delete(entity_index)
        return count

    def invalidate_org(self, org_id: Union[str, int]) -> int:
        """
        Invalidate all cache entries for an organization.
        
        Returns:
            Number of keys invalidated
        """
        org_index = self.INDEX_KEY.format(
            namespace=self.namespace, level=f"org:{org_id}"
        )
        keys = cache.get(org_index, set())
        
        count = 0
        for key in keys:
            cache.delete(key)
            count += 1
        
        cache.delete(org_index)
        return count

    def invalidate_resource(
        self, entity: str, resource_id: Union[str, int]
    ) -> int:
        """
        Invalidate all cache entries for a specific resource.
        
        Returns:
            Number of keys invalidated
        """
        resource_index = self.INDEX_KEY.format(
            namespace=self.namespace, level=f"resource:{resource_id}"
        )
        keys = cache.get(resource_index, set())
        
        count = 0
        for key in keys:
            cache.delete(key)
            count += 1
        
        cache.delete(resource_index)
        return count

    def invalidate_namespace(self) -> None:
        """
        Invalidate entire namespace by incrementing version.
        All subsequent builds with auto-fetched version will get new key paths.
        """
        version_key = self.NAMESPACE_VERSION_KEY.format(namespace=self.namespace)
        current = cache.get(version_key, 1)
        cache.set(version_key, current + 1, timeout=None)

    def get_all_versions(self) -> dict:
        """Get version info for this namespace."""
        return {
            "namespace": self.namespace,
            "current_version": self.get_version(),
            "ttl": self.ttl,
        }


class CacheKeyBuilder:
    """Helper class to build standard cache keys with patterns."""

    @staticmethod
    def feature_flag_key(org_id: Union[str, int], flag_key: str) -> str:
        """Build feature flag cache key: feature_flag:{org_id}:{flag_key}"""
        return f"feature_flag:{org_id}:{flag_key}"

    @staticmethod
    def org_features_key(org_id: Union[str, int]) -> str:
        """Build org-wide features cache key: features:{org_id}"""
        return f"features:{org_id}"

    @staticmethod
    def snapshot_key(
        resource_type: str,
        resource_id: Union[str, int],
        org_id: Optional[Union[str, int]] = None,
    ) -> str:
        """
        Build snapshot cache key.
        Format: snapshot:{resource_type}:{resource_id}[:{org_id}]
        """
        key = f"snapshot:{resource_type}:{resource_id}"
        if org_id:
            key += f":{org_id}"
        return key

    @staticmethod
    def snapshot_history_key(
        resource_type: str,
        resource_id: Union[str, int],
        org_id: Optional[Union[str, int]] = None,
    ) -> str:
        """Build snapshot history cache key."""
        key = f"snapshot_history:{resource_type}:{resource_id}"
        if org_id:
            key += f":{org_id}"
        return key

    @staticmethod
    def hash_key(data: str) -> str:
        """Generate deterministic hash key for data."""
        return hashlib.sha256(data.encode()).hexdigest()[:16]
