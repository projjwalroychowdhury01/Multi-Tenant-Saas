"""
Cross-tenant isolation test suite — Phase 1 placeholder.

These tests verify that Org A's authenticated tokens cannot access
Org B's data through any API endpoint.

Phase 1 has no tenant-scoped resource endpoints yet (those arrive in Phase 2+).
This file establishes the structure and will be populated endpoint-by-endpoint
as each Phase 2 resource is built.

RULE: Every test in this file MUST assert HTTP 404 (not 403)
      to prevent confirming resource existence to a foreign tenant.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.tenants.models import RoleEnum


@pytest.mark.django_db
class TestCrossTenantIsolation:
    """
    Stub isolation tests.
    Will grow as resource endpoints are added in Phases 2–5.
    """

    def setup_two_orgs(self):
        """Helper: creates two completely independent orgs each with an admin."""
        org_a = OrganizationFactory(name="Org Alpha")
        org_b = OrganizationFactory(name="Org Beta")

        user_a = UserFactory(password="TestPass123!")
        user_b = UserFactory(password="TestPass123!")

        MembershipFactory(organization=org_a, user=user_a, role=RoleEnum.ADMIN)
        MembershipFactory(organization=org_b, user=user_b, role=RoleEnum.ADMIN)

        return org_a, org_b, user_a, user_b

    def get_access_token(self, email, password) -> str:
        client = APIClient()
        res = client.post(
            "/auth/token",
            {"email": email, "password": password},
            format="json",
        )
        assert res.status_code == 200, f"Login failed for {email}: {res.data}"
        return res.data["access"]

    def test_stub_placeholder(self, db):
        """
        Isolation suite is wired up and ready.
        Phase 2 will populate concrete cross-tenant resource tests here.
        """
        org_a, org_b, user_a, user_b = self.setup_two_orgs()
        # Sanity check: both orgs are distinct
        assert org_a.id != org_b.id
        assert user_a.id != user_b.id
