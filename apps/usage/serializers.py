"""Usage serializers.

UsageSummarySerializer — aggregated API call usage for current billing period.
"""

from rest_framework import serializers


class UsageSummarySerializer(serializers.Serializer):
    """
    Response schema for GET /usage/summary.

    Shows the current billing period's API call consumption vs the plan limit.
    """

    metric_name = serializers.CharField(
        help_text="The metric being reported (e.g., 'api_calls')"
    )
    current_usage = serializers.IntegerField(
        help_text="Current consumption in the billing period"
    )
    limit = serializers.IntegerField(
        help_text="Plan limit for this metric"
    )
    percentage_used = serializers.FloatField(
        help_text="Percentage of limit consumed (0-100)"
    )
    period_start = serializers.DateTimeField(
        help_text="Start of the current billing period"
    )
    period_end = serializers.DateTimeField(
        help_text="End of the current billing period (when limit resets)"
    )
    is_in_grace_period = serializers.BooleanField(
        help_text="True if limit exceeded and in grace period"
    )
    grace_period_end = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="When grace period expires (if applicable)"
    )
