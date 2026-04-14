"""
Plan limit enforcement helpers.

`check_plan_limit(org, limit_type)` — raises PlanLimitExceeded if the org
has consumed more than their plan allows.

Grace period logic
──────────────────
When a limit is first exceeded, a 7-day grace window is opened on the
subscription. Requests are blocked only after this window expires. This
gives the org time to upgrade before enforcement is hard.

The grace window is reset to None when the org comes back under the limit.
"""

import logging
from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

logger = logging.getLogger(__name__)


# ── Exceptions ─────────────────────────────────────────────────────────────────


class PlanLimitExceeded(PermissionDenied):
    """
    Raised when an org has exceeded a plan quota and the grace period has
    elapsed (or was never set).

    Status code 403 so clients can distinguish from authentication errors.
    """

    default_code = "plan_limit_exceeded"


# ── Helpers ────────────────────────────────────────────────────────────────────

GRACE_PERIOD_DAYS = 7


def get_active_subscription(org):
    """
    Return the active Subscription for *org*, or None.

    Uses select_related to avoid an extra DB query for the plan.
    """
    from apps.billing.models import Subscription, SubscriptionStatus

    try:
        return (
            Subscription.objects
            .select_related("plan")
            .get(organization=org, status__in=[
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.PAST_DUE,
            ])
        )
    except Subscription.DoesNotExist:
        return None


def get_plan_limit(org, limit_type: str) -> int | None:
    """
    Return the numeric limit for *limit_type* from the org's active plan.

    Returns None if:
    - The org has no active subscription (no limit enforced)
    - The limit key is not defined on the plan (no limit enforced)
    """
    sub = get_active_subscription(org)
    if sub is None:
        return None
    return sub.plan.limits.get(limit_type)


def get_current_usage(org, limit_type: str) -> int:
    """
    Return the current usage count for *limit_type* for *org*.

    Counts are looked up from the live data:
    - ``members_count``      — OrganizationMembership rows
    - ``api_calls_per_month`` — UsageRecord rows for the current calendar month
    - ``storage_mb``         — UsageRecord rows for storge (placeholder: 0)
    """
    if limit_type == "members_count":
        from apps.tenants.models import OrganizationMembership
        return OrganizationMembership.objects.filter(organization=org).count()

    if limit_type == "api_calls_per_month":
        from apps.billing.models import UsageRecord
        from django.utils.timezone import now
        start_of_month = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        result = (
            UsageRecord.objects
            .filter(
                organization=org,
                metric_name="api_calls",
                period_start__gte=start_of_month,
            )
            .aggregate(total=__import__("django.db.models", fromlist=["Sum"]).Sum("quantity"))
        )
        return result["total"] or 0

    if limit_type == "storage_mb":
        # Phase 5 will wire this up to real storage tracking
        return 0

    logger.warning("get_current_usage: unknown limit_type %r", limit_type)
    return 0


def check_plan_limit(org, limit_type: str) -> None:
    """
    Raise PlanLimitExceeded if *org* has exceeded the *limit_type* quota
    AND their grace period has elapsed.

    Grace period workflow:
    1. First call where usage > limit: open a 7-day grace window on the sub.
    2. Subsequent calls within the window: log warning, allow the request.
    3. After the window expires: raise PlanLimitExceeded (hard block).
    4. If usage drops back under the limit: clear the grace window.
    """
    limit = get_plan_limit(org, limit_type)
    if limit is None:
        return  # no active subscription or limit not defined → allow

    usage = get_current_usage(org, limit_type)

    if usage <= limit:
        # Usage is within bounds — clear any stale grace period
        _clear_grace_period_if_set(org, limit_type)
        return

    # Usage is over the limit — check grace period
    sub = get_active_subscription(org)
    if sub is None:
        return

    if sub.grace_period_end is None:
        # First violation — open the grace window
        sub.grace_period_end = timezone.now() + timedelta(days=GRACE_PERIOD_DAYS)
        sub.save(update_fields=["grace_period_end", "updated_at"])
        logger.warning(
            "check_plan_limit: org %s exceeded %s (%d/%d). Grace period until %s.",
            org.id, limit_type, usage, limit, sub.grace_period_end,
        )
        return  # allow this request — grace period just started

    if sub.is_in_grace_period:
        logger.warning(
            "check_plan_limit: org %s over %s (%d/%d), grace ends %s.",
            org.id, limit_type, usage, limit, sub.grace_period_end,
        )
        return  # still within grace window

    # Grace period expired — hard block
    raise PlanLimitExceeded(
        f"Your organisation has exceeded the {limit_type!r} limit "
        f"({usage}/{limit}) and the 7-day grace period has elapsed. "
        f"Please upgrade your plan."
    )


def _clear_grace_period_if_set(org, limit_type: str) -> None:
    """Reset grace_period_end to None when usage is back within limits."""
    sub = get_active_subscription(org)
    if sub is not None and sub.grace_period_end is not None:
        sub.grace_period_end = None
        sub.save(update_fields=["grace_period_end", "updated_at"])
        logger.info(
            "check_plan_limit: org %s back under %s limit — grace period cleared.",
            org.id, limit_type,
        )
