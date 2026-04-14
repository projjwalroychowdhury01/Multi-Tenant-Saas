"""
Tests for GET /billing/plans and plan data integrity.
"""

import pytest

from tests.factories import PlanFactory

pytestmark = pytest.mark.django_db


class TestListPlans:
    URL = "/billing/plans/"

    def test_list_plans_is_public(self, client):
        """Unauthenticated users can list plans."""
        PlanFactory(name="Free Test", slug="free-test", price_monthly="0.00")
        response = client.get(self.URL)
        assert response.status_code == 200

    def test_returns_all_active_plans(self, client):
        PlanFactory(name="Starter", slug="starter", price_monthly="9.00")
        PlanFactory(name="Growth", slug="growth", price_monthly="29.00")
        # Inactive plan should not appear
        PlanFactory(name="Legacy", slug="legacy", is_active=False)

        response = client.get(self.URL)
        assert response.status_code == 200
        slugs = [p["slug"] for p in response.data]
        assert "starter" in slugs
        assert "growth" in slugs
        assert "legacy" not in slugs

    def test_plan_response_shape(self, client):
        PlanFactory(
            name="Pro",
            slug="pro-shape-test",
            price_monthly="49.00",
            limits={"members_count": 25},
            features={"audit_logs": True},
        )
        response = client.get(self.URL)
        plan = next(p for p in response.data if p["slug"] == "pro-shape-test")
        assert "id" in plan
        assert "name" in plan
        assert "slug" in plan
        assert "price_monthly" in plan
        assert "limits" in plan
        assert "features" in plan

    def test_ordered_by_price_ascending(self, client):
        PlanFactory(name="Expensive", slug="expensive-plan", price_monthly="99.00")
        PlanFactory(name="Cheap", slug="cheap-plan", price_monthly="1.00")

        response = client.get(self.URL)
        prices = [float(p["price_monthly"]) for p in response.data]
        assert prices == sorted(prices)

    def test_inactive_plans_excluded(self, client):
        PlanFactory(name="Hidden", slug="hidden-plan", is_active=False)
        response = client.get(self.URL)
        slugs = [p["slug"] for p in response.data]
        assert "hidden-plan" not in slugs
