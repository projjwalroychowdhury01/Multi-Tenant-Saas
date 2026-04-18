"""Usage metering integration tests."""

from datetime import datetime, timedelta

from django.utils import timezone

import pytest
from rest_framework import status

from apps.billing.models import Subscription, UsageRecord
from apps.tenants.models import RoleEnum
from tests.factories import (
    MembershipFactory,
    OrganizationFactory,
    PlanFactory,
    SubscriptionFactory,
    UserFactory,
)


@pytest.mark.django_db
class TestUsageSummary:
    """Tests for GET /usage/summary/ endpoint."""

    URL = "/usage/summary/"

    def test_get_usage_summary_success(self, auth_client, org, db):
        """Authenticated user with billing:read permission can view usage summary."""
        # Create a subscription with a plan that has api_calls_per_month limit
        plan = PlanFactory(limits={"api_calls_per_month": 1000})
        now = timezone.now()
        subscription = Subscription.objects.create(
            organization=org,
            plan=plan,
            current_period_start=now - timedelta(days=15),
            current_period_end=now + timedelta(days=15),
        )

        # Create some usage records for this org
        period_start = now - timedelta(hours=1)
        UsageRecord.objects.create(
            organization=org,
            metric_name="api_calls",
            quantity=350,
            period_start=period_start,
            period_end=period_start + timedelta(hours=1),
        )

        res = auth_client.get(self.URL)
        assert res.status_code == status.HTTP_200_OK
        assert res.data["metric_name"] == "api_calls"
        assert res.data["current_usage"] == 350
        assert res.data["limit"] == 1000
        assert res.data["percentage_used"] == 35.0
        assert "period_start" in res.data
        assert "period_end" in res.data
        assert res.data["is_in_grace_period"] is False

    def test_multiple_usage_records_aggregated(self, auth_client, org, db):
        """Usage from multiple periods is summed correctly."""
        plan = PlanFactory(limits={"api_calls_per_month": 1000})
        now = timezone.now()
        subscription = Subscription.objects.create(
            organization=org,
            plan=plan,
            current_period_start=now - timedelta(days=20),
            current_period_end=now + timedelta(days=10),
        )

        # Create multiple usage records in the current period
        for i in range(5):
            period_start = now - timedelta(hours=i)
            UsageRecord.objects.create(
                organization=org,
                metric_name="api_calls",
                quantity=100,
                period_start=period_start,
                period_end=period_start + timedelta(hours=1),
            )

        res = auth_client.get(self.URL)
        assert res.status_code == status.HTTP_200_OK
        assert res.data["current_usage"] == 500  # 5 * 100
        assert res.data["percentage_used"] == 50.0

    def test_no_subscription_returns_404(self, auth_client, org):
        """If org has no subscription, endpoint returns 404."""
        res = auth_client.get(self.URL)
        assert res.status_code == status.HTTP_404_NOT_FOUND
        assert res.data["code"] == "no_subscription"

    def test_zero_limit_shows_zero_percent(self, auth_client, org, db):
        """If plan has no limit, percentage shown is 0."""
        plan = PlanFactory(limits={})  # No api_calls_per_month
        now = timezone.now()
        Subscription.objects.create(
            organization=org,
            plan=plan,
            current_period_start=now - timedelta(days=15),
            current_period_end=now + timedelta(days=15),
        )

        res = auth_client.get(self.URL)
        assert res.status_code == status.HTTP_200_OK
        assert res.data["limit"] == 0
        assert res.data["percentage_used"] == 0.0

    def test_grace_period_info_included(self, auth_client, org, db):
        """Grace period information is included if applicable."""
        plan = PlanFactory(limits={"api_calls_per_month": 100})
        now = timezone.now()
        grace_end = now + timedelta(days=7)
        subscription = Subscription.objects.create(
            organization=org,
            plan=plan,
            current_period_start=now - timedelta(days=15),
            current_period_end=now + timedelta(days=15),
            grace_period_end=grace_end,
        )

        # Create usage that exceeds limit
        UsageRecord.objects.create(
            organization=org,
            metric_name="api_calls",
            quantity=150,
            period_start=now - timedelta(hours=1),
            period_end=now,
        )

        res = auth_client.get(self.URL)
        assert res.status_code == status.HTTP_200_OK
        assert res.data["is_in_grace_period"] is True
        assert "grace_period_end" in res.data

    def test_permission_required(self, db, api_client):
        """billing:read permission is required (VIEWER+ can view, MEMBER+ cannot)."""
        org = OrganizationFactory()

        # Test with MEMBER (no billing:read permission)
        member = UserFactory(password="Pass123!")
        MembershipFactory(organization=org, user=member, role=RoleEnum.MEMBER)

        res = api_client.post(
            "/auth/token",
            {"email": member.email, "password": "Pass123!"},
            format="json",
        )
        token = res.data["access"]
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        res = api_client.get(self.URL)
        assert res.status_code == status.HTTP_403_FORBIDDEN

        # Test with VIEWER (has billing:read permission)
        viewer = UserFactory(password="Pass123!")
        MembershipFactory(organization=org, user=viewer, role=RoleEnum.VIEWER)

        res = api_client.post(
            "/auth/token",
            {"email": viewer.email, "password": "Pass123!"},
            format="json",
        )
        token = res.data["access"]
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        # Create subscription first
        plan = PlanFactory(limits={"api_calls_per_month": 1000})
        now = timezone.now()
        Subscription.objects.create(
            organization=org,
            plan=plan,
            current_period_start=now - timedelta(days=15),
            current_period_end=now + timedelta(days=15),
        )

        res = api_client.get(self.URL)
        assert res.status_code == status.HTTP_200_OK

    def test_cross_tenant_isolation(self, db, api_client):
        """Org B's usage is not visible to Org A."""
        org_a = OrganizationFactory()
        org_b = OrganizationFactory()

        admin_a = UserFactory(password="Pass123!")
        MembershipFactory(organization=org_a, user=admin_a, role=RoleEnum.ADMIN)

        # Create subscription and usage for org_a
        plan = PlanFactory(limits={"api_calls_per_month": 1000})
        now = timezone.now()
        sub_a = Subscription.objects.create(
            organization=org_a,
            plan=plan,
            current_period_start=now - timedelta(days=15),
            current_period_end=now + timedelta(days=15),
        )
        UsageRecord.objects.create(
            organization=org_a,
            metric_name="api_calls",
            quantity=500,
            period_start=now - timedelta(hours=1),
            period_end=now,
        )

        # Create subscription and usage for org_b
        sub_b = Subscription.objects.create(
            organization=org_b,
            plan=plan,
            current_period_start=now - timedelta(days=15),
            current_period_end=now + timedelta(days=15),
        )
        UsageRecord.objects.create(
            organization=org_b,
            metric_name="api_calls",
            quantity=100,
            period_start=now - timedelta(hours=1),
            period_end=now,
        )

        # Admin A logs in and checks usage
        res = api_client.post(
            "/auth/token",
            {"email": admin_a.email, "password": "Pass123!"},
            format="json",
        )
        token = res.data["access"]
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        res = api_client.get(self.URL)
        assert res.status_code == status.HTTP_200_OK
        assert res.data["current_usage"] == 500  # Only org_a's usage
        assert res.data["limit"] == 1000

    def test_unauthenticated_access_denied(self, api_client):
        """Unauthenticated users cannot access usage summary."""
        res = api_client.get(self.URL)
        assert res.status_code == status.HTTP_401_UNAUTHORIZED
