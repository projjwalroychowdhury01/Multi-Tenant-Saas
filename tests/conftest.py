"""
Shared pytest fixtures.

All tests import from here via conftest auto-discovery.
"""

import pytest
from rest_framework.test import APIClient

from tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.tenants.models import RoleEnum


@pytest.fixture
def api_client():
    """Unauthenticated DRF test client."""
    return APIClient()


@pytest.fixture
def org(db):
    """A fresh Organization for each test that needs it."""
    return OrganizationFactory()


@pytest.fixture
def user(db):
    """A fresh User for each test that needs it (no org membership)."""
    return UserFactory(password="TestPass123!")


@pytest.fixture
def owner(db, org):
    """User with OWNER role in `org`."""
    u = UserFactory(password="TestPass123!")
    MembershipFactory(organization=org, user=u, role=RoleEnum.OWNER)
    return u


@pytest.fixture
def admin_user(db, org):
    """User with ADMIN role in `org`."""
    u = UserFactory(password="TestPass123!")
    MembershipFactory(organization=org, user=u, role=RoleEnum.ADMIN)
    return u


@pytest.fixture
def member_user(db, org):
    """User with MEMBER role in `org`."""
    u = UserFactory(password="TestPass123!")
    MembershipFactory(organization=org, user=u, role=RoleEnum.MEMBER)
    return u


@pytest.fixture
def auth_client(api_client, owner, org):
    """APIClient pre-authenticated as the org owner using JWT."""
    res = api_client.post(
        "/auth/token",
        {"email": owner.email, "password": "TestPass123!"},
        format="json",
    )
    assert res.status_code == 200, f"Login failed: {res.data}"
    token = res.data["access"]
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    api_client.org = org
    api_client.user = owner
    return api_client
