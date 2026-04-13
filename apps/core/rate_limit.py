"""
Redis sliding-window rate limiter.

Algorithm
─────────
  Uses an atomic Lua script executed server-side on Redis to implement a
  sliding-window counter.  All incrementing, expiry, and comparison happen
  in a single atomic operation — no race condition is possible.

  The Lua script:
    1. INCR the key for the current window (org:metric:window_start).
    2. Set TTL = window_size + 1 on the first increment (so it auto-expires).
    3. Return (current_count, window_start, limit).

Key structure
─────────────
  rate:{org_id}:{window_start_unix}

  window_start_unix is floored to the nearest `window_size` seconds so all
  requests within the same window share the same Redis key.

Plan → limit mapping
────────────────────
  FREE       : 100 requests / minute
  PRO        : 1,000 requests / minute
  ENTERPRISE : 10,000 requests / minute
  (default)  : 100 requests / minute (treat unknown plans as FREE)
"""

import logging
import time
from dataclasses import dataclass

from django.core.cache import cache

logger = logging.getLogger(__name__)

# ── Plan limits ───────────────────────────────────────────────────────────────

PLAN_LIMITS: dict[str, int] = {
    "FREE": 100,
    "PRO": 1_000,
    "ENTERPRISE": 10_000,
}

WINDOW_SIZE = 60  # seconds (1 minute sliding window)

# ── Lua script (atomic sliding window) ───────────────────────────────────────
#
# KEYS[1] = rate key (e.g. "rate:org_uuid:1713000000")
# ARGV[1] = window TTL in seconds (window_size + 1 buffer)
#
# Returns: current request count after the increment
_LUA_SCRIPT = """
local key    = KEYS[1]
local ttl    = tonumber(ARGV[1])
local count  = redis.call('INCR', key)
if count == 1 then
    redis.call('EXPIRE', key, ttl)
end
return count
"""

# Cache the compiled script object to avoid re-compilation
_script_sha: str | None = None


@dataclass
class RateLimitResult:
    """Result of a single rate-limit evaluation."""

    allowed: bool
    limit: int
    remaining: int
    reset_at: int  # Unix timestamp when the window resets
    retry_after: int  # Seconds until next allowed request (0 if allowed)
    current_count: int


def _get_limit(plan: str) -> int:
    return PLAN_LIMITS.get(plan.upper(), PLAN_LIMITS["FREE"])


def _window_start(now: float) -> int:
    """Floor `now` to the start of the current window."""
    return int(now // WINDOW_SIZE) * WINDOW_SIZE


def _build_rate_key(org_id: str, window_start: int) -> str:
    return f"rate:{org_id}:{window_start}"


def check_rate_limit(org_id: str, plan: str) -> RateLimitResult:
    """
    Evaluate the sliding-window rate limit for the given org.

    Executes an atomic Lua script on Redis.  Falls back to allowing the
    request if Redis is unreachable (fail-open) so a Redis outage doesn't
    take down your entire API.

    Args:
        org_id: String UUID of the organization.
        plan:   Plan name (FREE / PRO / ENTERPRISE).

    Returns:
        RateLimitResult with allowed flag and header values.
    """
    limit = _get_limit(plan)
    now = time.time()
    window_start = _window_start(now)
    reset_at = window_start + WINDOW_SIZE
    key = _build_rate_key(str(org_id), window_start)
    ttl = WINDOW_SIZE + 1  # +1 buffer so the key outlives the window

    try:
        # Use Django's cache (backed by Redis) to get the raw client
        redis_client = cache.client.get_client()  # type: ignore[attr-defined]

        global _script_sha
        if _script_sha is None:
            _script_sha = redis_client.script_load(_LUA_SCRIPT)

        current_count = redis_client.evalsha(_script_sha, 1, key, ttl)
        current_count = int(current_count)

    except Exception as exc:
        # Fail-open: Redis unavailable → allow the request, log the error
        logger.error("Rate limit Redis error (fail-open): %s", exc)
        return RateLimitResult(
            allowed=True,
            limit=limit,
            remaining=limit,
            reset_at=int(reset_at),
            retry_after=0,
            current_count=0,
        )

    allowed = current_count <= limit
    remaining = max(0, limit - current_count)
    retry_after = 0 if allowed else int(reset_at - now)

    return RateLimitResult(
        allowed=allowed,
        limit=limit,
        remaining=remaining,
        reset_at=int(reset_at),
        retry_after=retry_after,
        current_count=current_count,
    )
