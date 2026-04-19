"""
Usage metering views.

GET /usage/summary — current billing period's API call usage vs plan limit
"""

import logging
from datetime import timedelta

from django.db.models import Sum

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.billing.models import Subscription, UsageRecord
from apps.rbac.permissions import CanReadBilling
from apps.usage.serializers import UsageSummarySerializer

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([IsAuthenticated, CanReadBilling])
def get_usage_summary(request):
    """
    Return the current billing period's API call usage vs plan limit.

    Requires ``billing:read`` permission (VIEWER+, ADMIN, OWNER, BILLING).

    Response includes:
      - current_usage: API calls consumed in the current period
      - limit: plan's api_calls_per_month limit
      - percentage_used: current_usage / limit * 100
      - period_start/end: current billing period boundaries
      - is_in_grace_period: True if limit exceeded and grace period is active
      - grace_period_end: when grace period expires (if applicable)
    """
    org = getattr(request, "org", None)
    if org is None:
        return Response(
            {"error": "No active organisation context.", "code": "no_org"},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        subscription = Subscription.objects.select_related("plan").get(organization=org)
    except Subscription.DoesNotExist:
        return Response(
            {"error": "No active subscription found.", "code": "no_subscription"},
            status=status.HTTP_404_NOT_FOUND,
        )

    # ── Determine billing period ──────────────────────────────────────────────
    # Simple model: billing period is subscription's current_period_start to
    # current_period_end. In a real Stripe integration, this would come from
    # the subscription object itself.

    period_start = subscription.current_period_start
    period_end = subscription.current_period_end

    # Fallback if period_end is not set (shouldn't happen in production)
    if not period_end:
        period_end = period_start + timedelta(days=30)

    # ── Sum usage for the current period ──────────────────────────────────────
    usage_sum = UsageRecord.objects.filter(
        organization=org,
        metric_name="api_calls",
        period_start__gte=period_start,
        period_start__lt=period_end,
    ).aggregate(total=Sum("quantity"))
    current_usage = usage_sum["total"] or 0

    # ── Extract plan limit ────────────────────────────────────────────────────
    plan_limits = subscription.plan.limits or {}
    limit = plan_limits.get("api_calls_per_month", 0)

    # ── Calculate percentage consumed ─────────────────────────────────────────
    if limit > 0:
        percentage_used = (current_usage / limit) * 100
    else:
        percentage_used = 0.0

    # ── Grace period info ─────────────────────────────────────────────────────
    is_in_grace_period = subscription.is_in_grace_period
    grace_period_end = subscription.grace_period_end

    # ── Serialize and return ──────────────────────────────────────────────────
    data = {
        "metric_name": "api_calls",
        "current_usage": current_usage,
        "limit": limit,
        "percentage_used": percentage_used,
        "period_start": period_start,
        "period_end": period_end,
        "is_in_grace_period": is_in_grace_period,
    }
    if grace_period_end:
        data["grace_period_end"] = grace_period_end

    serializer = UsageSummarySerializer(data)
    return Response(serializer.data)
