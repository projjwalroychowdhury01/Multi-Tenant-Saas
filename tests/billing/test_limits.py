"""
Tests for plan limit enforcement, feature gates, and usage threshold alerts.

Covers:
- check_plan_limit allows when under limit
- First violation opens 7-day grace period (request still allowed)
- Request within grace period is allowed
- Request after grace period expires → PlanLimitExceeded
- Usage back under limit clears grace period
- is_feature_enabled returns True/False based on plan.features
- check_feature_gate raises FeatureNotAvailable for disabled features
- 80 % usage fires warning alert task
- 100 % usage fires critical alert task
"""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest

from apps.billing.limits import (
    GRACE_PERIOD_DAYS,
    FeatureNotAvailable,
    PlanLimitExceeded,
    check_feature_gate,
    check_plan_limit,
    get_active_subscription,
    get_plan_limit,
    is_feature_enabled,
)
from apps.tenants.models import RoleEnum
from tests.factories import (
    MembershipFactory,
    OrganizationFactory,
    PlanFactory,
    SubscriptionFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def org_with_plan(db):
    """Return (org, subscription, plan) with members_count limit of 2."""
    org = OrganizationFactory()
    plan = PlanFactory(
        slug="limit-test-plan",
        name="Limit Test",
        limits={"members_count": 2, "api_calls_per_month": 1000, "storage_mb": 512},
        features={"audit_logs": True, "feature_flags": False, "sso": False},
    )
    sub = SubscriptionFactory(organization=org, plan=plan)
    return org, sub, plan


class TestGetActiveSubscription:
    def test_returns_active_subscription(self, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="active-sub-test", name="Active Sub")
        sub = SubscriptionFactory(organization=org, plan=plan, status="active")
        result = get_active_subscription(org)
        assert result is not None
        assert result.id == sub.id

    def test_returns_past_due_subscription(self, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="past-due-test", name="Past Due")
        SubscriptionFactory(organization=org, plan=plan, status="past_due")
        result = get_active_subscription(org)
        assert result is not None

    def test_returns_none_for_canceled(self, db):
        org = OrganizationFactory()
        plan = PlanFactory(slug="canceled-sub-test", name="Canceled Sub")
        SubscriptionFactory(organization=org, plan=plan, status="canceled")
        result = get_active_subscription(org)
        assert result is None

    def test_returns_none_when_no_subscription(self, db):
        org = OrganizationFactory()
        result = get_active_subscription(org)
        assert result is None


class TestGetPlanLimit:
    def test_returns_limit_from_plan(self, org_with_plan):
        org, sub, plan = org_with_plan
        assert get_plan_limit(org, "members_count") == 2

    def test_returns_none_when_no_subscription(self, db):
        org = OrganizationFactory()
        assert get_plan_limit(org, "members_count") is None


class TestCheckPlanLimit:
    def test_allows_when_under_limit(self, org_with_plan):
        org, sub, plan = org_with_plan
        # 0 members currently — well under limit of 2
        check_plan_limit(org, "members_count")  # should not raise

    def test_allows_when_no_subscription(self, db):
        org = OrganizationFactory()
        check_plan_limit(org, "members_count")  # no subscription = no limit

    def test_first_violation_opens_grace_period(self, org_with_plan):
        """First time limit is exceeded: grace period opens, request allowed."""
        org, sub, plan = org_with_plan
        # Add 3 members (limit is 2)
        for _ in range(3):
            MembershipFactory(organization=org, user=UserFactory(), role=RoleEnum.MEMBER)

        with patch("apps.billing.limits._fire_threshold_alert") as mock_alert:
            # Should NOT raise — grace period just opened
            check_plan_limit(org, "members_count")
            # Critical alert dispatched on first violation
            mock_alert.assert_called_once_with(org, "members_count", 3, 2, severity="critical")

        sub.refresh_from_db()
        assert sub.grace_period_end is not None
        assert sub.grace_period_end > timezone.now()

    def test_within_grace_period_is_allowed(self, org_with_plan):
        """Requests within the grace window are allowed."""
        org, sub, plan = org_with_plan
        for _ in range(3):
            MembershipFactory(organization=org, user=UserFactory(), role=RoleEnum.MEMBER)

        # Set grace period to future
        sub.grace_period_end = timezone.now() + timedelta(days=3)
        sub.save()

        check_plan_limit(org, "members_count")  # should not raise

    def test_expired_grace_period_raises(self, org_with_plan):
        """After grace period expires, PlanLimitExceeded is raised."""
        org, sub, plan = org_with_plan
        for _ in range(3):
            MembershipFactory(organization=org, user=UserFactory(), role=RoleEnum.MEMBER)

        # Set grace period to the past
        sub.grace_period_end = timezone.now() - timedelta(days=1)
        sub.save()

        with pytest.raises(PlanLimitExceeded):
            check_plan_limit(org, "members_count")

    def test_coming_under_limit_clears_grace_period(self, org_with_plan):
        """When usage drops below limit, grace_period_end is reset to None."""
        org, sub, plan = org_with_plan
        # Set a stale grace period
        sub.grace_period_end = timezone.now() + timedelta(days=5)
        sub.save()

        # 0 members — under limit of 2
        check_plan_limit(org, "members_count")

        sub.refresh_from_db()
        assert sub.grace_period_end is None

    def test_grace_period_duration_is_7_days(self, org_with_plan):
        org, sub, plan = org_with_plan
        for _ in range(3):
            MembershipFactory(organization=org, user=UserFactory(), role=RoleEnum.MEMBER)

        before = timezone.now()
        with patch("apps.billing.limits._fire_threshold_alert"):
            check_plan_limit(org, "members_count")
        after = timezone.now()

        sub.refresh_from_db()
        # Grace period should be approximately 7 days from now
        expected_min = before + timedelta(days=GRACE_PERIOD_DAYS - 1)
        expected_max = after + timedelta(days=GRACE_PERIOD_DAYS + 1)
        assert expected_min < sub.grace_period_end < expected_max

    def test_warning_alert_fired_at_80_percent(self, org_with_plan):
        """80 % usage fires a warning alert via _fire_threshold_alert."""
        org, sub, plan = org_with_plan
        # Plan limit is 2 — adding 2 members = 100 %, but we patch usage to 1.6
        # (effectively 80 %) to test the warning threshold without DB gymnastics.
        with patch("apps.billing.limits.get_current_usage", return_value=1):
            # usage=1, limit=2 → 50 %; no alert
            with patch("apps.billing.limits._fire_threshold_alert") as mock_alert:
                check_plan_limit(org, "members_count")
                mock_alert.assert_not_called()

        # Simulate 80 % usage (2 out of 2 is 100 %, use a bigger limit plan)
        plan_80 = PlanFactory(
            slug="limit-80-test",
            name="Limit 80",
            limits={"members_count": 10, "api_calls_per_month": 1000, "storage_mb": 512},
        )
        org_80 = OrganizationFactory()
        SubscriptionFactory(organization=org_80, plan=plan_80)
        for _ in range(8):
            MembershipFactory(organization=org_80, user=UserFactory(), role=RoleEnum.MEMBER)

        with patch("apps.billing.limits._fire_threshold_alert") as mock_alert:
            check_plan_limit(org_80, "members_count")  # 8/10 = 80 % → warning
            mock_alert.assert_called_once_with(org_80, "members_count", 8, 10, severity="warning")


class TestIsFeatureEnabled:
    def test_returns_true_for_enabled_feature(self, org_with_plan):
        org, sub, plan = org_with_plan
        # plan.features has audit_logs=True
        assert is_feature_enabled(org, "audit_logs") is True

    def test_returns_false_for_disabled_feature(self, org_with_plan):
        org, sub, plan = org_with_plan
        # plan.features has feature_flags=False
        assert is_feature_enabled(org, "feature_flags") is False

    def test_returns_false_for_missing_key(self, org_with_plan):
        org, sub, plan = org_with_plan
        # "nonexistent_feature" is not in features dict
        assert is_feature_enabled(org, "nonexistent_feature") is False

    def test_returns_false_when_no_subscription(self, db):
        org = OrganizationFactory()
        assert is_feature_enabled(org, "audit_logs") is False

    def test_returns_true_for_enterprise_sso(self, db):
        org = OrganizationFactory()
        enterprise_plan = PlanFactory(
            slug="enterprise-test",
            name="Enterprise",
            features={"audit_logs": True, "feature_flags": True, "sso": True},
        )
        SubscriptionFactory(organization=org, plan=enterprise_plan)
        assert is_feature_enabled(org, "sso") is True


class TestCheckFeatureGate:
    def test_passes_for_enabled_feature(self, org_with_plan):
        org, sub, plan = org_with_plan
        # audit_logs is True on this plan — should not raise
        check_feature_gate(org, "audit_logs")

    def test_raises_for_disabled_feature(self, org_with_plan):
        org, sub, plan = org_with_plan
        # feature_flags is False on this plan
        with pytest.raises(FeatureNotAvailable) as exc_info:
            check_feature_gate(org, "feature_flags")
        assert exc_info.value.default_code == "feature_not_available"
        assert "feature_flags" in str(exc_info.value.detail)

    def test_raises_with_plan_name_in_message(self, org_with_plan):
        org, sub, plan = org_with_plan
        with pytest.raises(FeatureNotAvailable) as exc_info:
            check_feature_gate(org, "sso")
        # Plan name should appear in the error message
        assert plan.name in str(exc_info.value.detail)

    def test_raises_when_no_subscription(self, db):
        org = OrganizationFactory()
        with pytest.raises(FeatureNotAvailable):
            check_feature_gate(org, "audit_logs")

    def test_error_code_is_feature_not_available(self, org_with_plan):
        org, sub, plan = org_with_plan
        with pytest.raises(FeatureNotAvailable) as exc_info:
            check_feature_gate(org, "sso")
        assert exc_info.value.default_code == "feature_not_available"
