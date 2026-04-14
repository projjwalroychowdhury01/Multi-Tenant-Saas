"""
Tests for the sliding-window rate limiter.

Since tests use LocMemCache (no real Redis) and RATE_LIMIT_ENABLED=False,
we test:
  1. The pure logic of check_rate_limit() with a mock Redis client.
  2. That the RateLimitMiddleware correctly injects headers.
  3. That HTTP 429 is returned with the right shape when over-limit.
  4. That plan-tier limits are enforced correctly.
  5. That unauthenticated requests bypass rate limiting.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from apps.core.rate_limit import (
    PLAN_LIMITS,
    RateLimitResult,
    _get_limit,
    _window_start,
    check_rate_limit,
)


# ── Pure logic tests (no Redis required) ──────────────────────────────────────

class TestRateLimitLogic:
    def test_get_limit_free(self):
        assert _get_limit("FREE") == 100

    def test_get_limit_pro(self):
        assert _get_limit("PRO") == 1_000

    def test_get_limit_enterprise(self):
        assert _get_limit("ENTERPRISE") == 10_000

    def test_get_limit_unknown_defaults_to_free(self):
        assert _get_limit("UNKNOWN_PLAN") == PLAN_LIMITS["FREE"]

    def test_get_limit_case_insensitive(self):
        assert _get_limit("pro") == 1_000
        assert _get_limit("Pro") == 1_000

    def test_window_start_is_floor_of_window_size(self):
        # Window size = 60s; t=75 → window_start=60
        ws = _window_start(75.0)
        assert ws == 60

    def test_window_start_at_boundary(self):
        ws = _window_start(120.0)
        assert ws == 120

    def test_plan_limits_coverage(self):
        """Verify all expected plan tiers are present."""
        for plan in ["FREE", "PRO", "ENTERPRISE"]:
            assert plan in PLAN_LIMITS


class TestCheckRateLimitWithMockRedis:
    """
    Test check_rate_limit() using a mock Redis client so tests don't
    need a real Redis instance.
    """

    def _mock_redis(self, count: int):
        """Return a mock Redis client that always returns `count` on evalsha."""
        client = MagicMock()
        client.script_load.return_value = "fake_sha"
        client.evalsha.return_value = count
        return client

    def _patch_redis(self, count: int):
        redis_mock = self._mock_redis(count)
        cache_mock = MagicMock()
        cache_mock.client.get_client.return_value = redis_mock
        return cache_mock, redis_mock

    def test_under_limit_returns_allowed(self):
        cache_mock, _ = self._patch_redis(count=50)
        with patch("apps.core.rate_limit.cache", cache_mock):
            result = check_rate_limit("org-1", "FREE")
        assert result.allowed is True
        assert result.remaining == 50  # 100 - 50
        assert result.limit == 100

    def test_at_limit_returns_allowed(self):
        cache_mock, _ = self._patch_redis(count=100)
        with patch("apps.core.rate_limit.cache", cache_mock):
            result = check_rate_limit("org-1", "FREE")
        assert result.allowed is True
        assert result.remaining == 0

    def test_over_limit_returns_denied(self):
        cache_mock, _ = self._patch_redis(count=101)
        with patch("apps.core.rate_limit.cache", cache_mock):
            result = check_rate_limit("org-1", "FREE")
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after > 0

    def test_pro_plan_allows_more(self):
        cache_mock, _ = self._patch_redis(count=500)
        with patch("apps.core.rate_limit.cache", cache_mock):
            result = check_rate_limit("org-1", "PRO")
        assert result.allowed is True
        assert result.remaining == 500  # 1000 - 500

    def test_pro_limit_exceeded(self):
        cache_mock, _ = self._patch_redis(count=1001)
        with patch("apps.core.rate_limit.cache", cache_mock):
            result = check_rate_limit("org-1", "PRO")
        assert result.allowed is False

    def test_enterprise_plan_allows_up_to_10000(self):
        cache_mock, _ = self._patch_redis(count=9_999)
        with patch("apps.core.rate_limit.cache", cache_mock):
            result = check_rate_limit("org-1", "ENTERPRISE")
        assert result.allowed is True

    def test_reset_at_is_in_the_future(self):
        cache_mock, _ = self._patch_redis(count=1)
        with patch("apps.core.rate_limit.cache", cache_mock):
            result = check_rate_limit("org-1", "FREE")
        assert result.reset_at > int(time.time())

    def test_redis_failure_fails_open(self):
        """If Redis is unavailable, the request should be allowed (fail-open)."""
        cache_mock = MagicMock()
        cache_mock.client.get_client.side_effect = Exception("Redis connection refused")
        with patch("apps.core.rate_limit.cache", cache_mock):
            result = check_rate_limit("org-1", "FREE")
        assert result.allowed is True
        assert result.remaining == 100


# ── Middleware integration tests ───────────────────────────────────────────────

class TestRateLimitMiddleware:
    """
    Test the RateLimitMiddleware via the Django test client.

    Rate limiting is disabled globally in testing.py (RATE_LIMIT_ENABLED=False),
    so these tests re-enable it via settings override.
    """

    @pytest.mark.django_db
    @override_settings(RATE_LIMIT_ENABLED=True)
    def test_rate_limit_headers_present_when_enabled(self, auth_client):
        """Headers should be injected when middleware is active."""
        allowed_result = RateLimitResult(
            allowed=True, limit=100, remaining=99, reset_at=9999999999, retry_after=0, current_count=1
        )
        with patch("apps.core.rate_limit.check_rate_limit", return_value=allowed_result):
            res = auth_client.get("/auth/me")
        assert "X-RateLimit-Limit" in res
        assert "X-RateLimit-Remaining" in res
        assert "X-RateLimit-Reset" in res
        assert res["X-RateLimit-Limit"] == "100"
        assert res["X-RateLimit-Remaining"] == "99"

    @pytest.mark.django_db
    @override_settings(RATE_LIMIT_ENABLED=True)
    def test_429_returned_when_limit_exceeded(self, auth_client):
        """Middleware must block the request and return 429 with Retry-After."""
        denied_result = RateLimitResult(
            allowed=False, limit=100, remaining=0, reset_at=9999999999, retry_after=45, current_count=101
        )
        with patch("apps.core.rate_limit.check_rate_limit", return_value=denied_result):
            res = auth_client.get("/auth/me")
        assert res.status_code == 429
        assert res["Retry-After"] == "45"
        assert res.json()["code"] == "rate_limit_exceeded"

    @pytest.mark.django_db
    @override_settings(RATE_LIMIT_ENABLED=True)
    def test_unauthenticated_requests_bypass_rate_limit(self, api_client):
        """Requests with no org context must not be rate-limited."""
        res = api_client.get("/health")
        # Should reach the view — not blocked by rate limiter
        assert res.status_code == 200
        # No rate limit headers on public endpoints
        assert "X-RateLimit-Limit" not in res
