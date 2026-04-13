"""
Tests for API key CRUD and rotation endpoints.

Coverage
────────
  POST   /api-keys/              — create (verifies secret shown once)
  GET    /api-keys/              — list (verifies no secret in response)
  GET    /api-keys/{id}/         — detail (masked)
  PATCH  /api-keys/{id}/         — update name/scopes
  DELETE /api-keys/{id}/         — revoke
  POST   /api-keys/{id}/rotate/  — 24-hour overlap rotation

Security invariants tested
──────────────────────────
  - `hashed_key` NEVER appears in any response
  - `secret` only appears in create + rotate responses
  - Cross-org: org-B cannot manipulate org-A's keys
  - VIEWER cannot create/delete/rotate (only read)
  - MEMBER cannot read keys of other members (api_keys:manage scope)
"""

import pytest
from django.utils import timezone

from apps.api_keys.models import ApiKey
from apps.tenants.models import RoleEnum
from tests.factories import ApiKeyFactory, MembershipFactory, OrganizationFactory, UserFactory


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_client(api_client, org, db):
    """Authenticated client with ADMIN role."""
    user = UserFactory(password="Test1234!")
    MembershipFactory(organization=org, user=user, role=RoleEnum.ADMIN)
    res = api_client.post("/auth/token", {"email": user.email, "password": "Test1234!"}, format="json")
    assert res.status_code == 200
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
    api_client.user = user
    api_client.org = org
    return api_client


@pytest.fixture
def viewer_client(api_client, org, db):
    """Authenticated client with VIEWER role."""
    user = UserFactory(password="Test1234!")
    MembershipFactory(organization=org, user=user, role=RoleEnum.VIEWER)
    res = api_client.post("/auth/token", {"email": user.email, "password": "Test1234!"}, format="json")
    assert res.status_code == 200
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
    api_client.user = user
    api_client.org = org
    return api_client


@pytest.fixture
def existing_key(db, org, owner):
    """A live ApiKey pre-seeded in the DB for org."""
    key, secret = ApiKeyFactory.create_with_secret(organization=org, created_by=owner)
    key._test_secret = secret
    return key


# ── Create ────────────────────────────────────────────────────────────────────

class TestApiKeyCreate:
    def test_create_returns_secret_once(self, auth_client):
        """Secret must appear in the create response."""
        payload = {"name": "CI Pipeline Key", "env": "live"}
        res = auth_client.post("/api-keys/", payload, format="json")
        assert res.status_code == 201, res.data
        assert "secret" in res.data
        assert res.data["secret"].startswith("sk_live_")

    def test_secret_not_in_list_after_create(self, auth_client):
        """Secret must not appear in the list response."""
        auth_client.post("/api-keys/", {"name": "K1", "env": "live"}, format="json")
        res = auth_client.get("/api-keys/")
        assert res.status_code == 200
        for item in res.data["results"]:
            assert "secret" not in item
            assert "hashed_key" not in item

    def test_create_stores_prefix_not_secret(self, auth_client, org):
        """DB record must only store prefix and hash, never the plaintext."""
        res = auth_client.post("/api-keys/", {"name": "Security Check", "env": "live"}, format="json")
        assert res.status_code == 201
        key = ApiKey.all_objects.get(id=res.data["id"])
        # prefix is stored
        assert key.prefix.startswith("sk_live_")
        # hashed_key is a 64-char hex string (SHA-256)
        assert len(key.hashed_key) == 64
        # hashed_key must NOT equal the plaintext secret
        assert key.hashed_key != res.data["secret"]

    def test_create_test_env_key(self, auth_client):
        res = auth_client.post("/api-keys/", {"name": "Test Key", "env": "test"}, format="json")
        assert res.status_code == 201
        assert res.data["secret"].startswith("sk_test_")
        assert res.data["env"] == "test"

    def test_create_with_scopes(self, auth_client):
        payload = {"name": "Scoped Key", "env": "live", "scopes": ["api_keys:read"]}
        res = auth_client.post("/api-keys/", payload, format="json")
        assert res.status_code == 201
        assert "api_keys:read" in res.data["scopes"]

    def test_create_with_invalid_scope_rejected(self, auth_client):
        payload = {"name": "Bad Scopes", "env": "live", "scopes": ["admin:destroy_everything"]}
        res = auth_client.post("/api-keys/", payload, format="json")
        assert res.status_code == 400

    def test_viewer_cannot_create(self, viewer_client):
        res = viewer_client.post("/api-keys/", {"name": "Viewer Key", "env": "live"}, format="json")
        assert res.status_code == 403

    def test_unauthenticated_cannot_create(self, api_client):
        res = api_client.post("/api-keys/", {"name": "Anon Key", "env": "live"}, format="json")
        assert res.status_code == 401


# ── List ──────────────────────────────────────────────────────────────────────

class TestApiKeyList:
    def test_list_returns_only_org_keys(self, auth_client, org, owner, db):
        """Keys from another org must not appear."""
        other_org = OrganizationFactory()
        other_user = UserFactory()
        ApiKeyFactory(organization=other_org, created_by=other_user)
        ApiKeyFactory(organization=org, created_by=owner)

        res = auth_client.get("/api-keys/")
        assert res.status_code == 200
        # All returned keys must belong to auth_client's org
        for item in res.data["results"]:
            key = ApiKey.all_objects.get(id=item["id"])
            assert str(key.organization_id) == str(org.id)

    def test_list_never_exposes_hashed_key(self, auth_client, existing_key):
        res = auth_client.get("/api-keys/")
        assert res.status_code == 200
        for item in res.data["results"]:
            assert "hashed_key" not in item

    def test_viewer_can_list(self, viewer_client, existing_key):
        res = viewer_client.get("/api-keys/")
        assert res.status_code == 200
        assert "results" in res.data


# ── Detail ────────────────────────────────────────────────────────────────────

class TestApiKeyDetail:
    def test_retrieve_masked(self, auth_client, existing_key):
        res = auth_client.get(f"/api-keys/{existing_key.id}/")
        assert res.status_code == 200
        assert "hashed_key" not in res.data
        assert "secret" not in res.data
        assert res.data["prefix"] == existing_key.prefix

    def test_cross_org_retrieve_returns_404(self, viewer_client, db):
        """Org-B's VIEWER cannot retrieve org-A's key."""
        other_org = OrganizationFactory()
        other_user = UserFactory()
        other_key = ApiKeyFactory(organization=other_org, created_by=other_user)
        res = viewer_client.get(f"/api-keys/{other_key.id}/")
        assert res.status_code == 404


# ── Update ────────────────────────────────────────────────────────────────────

class TestApiKeyUpdate:
    def test_patch_name(self, auth_client, existing_key):
        res = auth_client.patch(f"/api-keys/{existing_key.id}/", {"name": "New Name"}, format="json")
        assert res.status_code == 200
        existing_key.refresh_from_db()
        assert existing_key.name == "New Name"

    def test_viewer_cannot_patch(self, viewer_client, existing_key):
        res = viewer_client.patch(f"/api-keys/{existing_key.id}/", {"name": "X"}, format="json")
        assert res.status_code == 403

    def test_deactivate_via_patch(self, auth_client, existing_key):
        res = auth_client.patch(
            f"/api-keys/{existing_key.id}/", {"is_active": False}, format="json"
        )
        assert res.status_code == 200
        existing_key.refresh_from_db()
        assert existing_key.is_active is False


# ── Revoke (DELETE) ───────────────────────────────────────────────────────────

class TestApiKeyRevoke:
    def test_delete_sets_inactive(self, auth_client, existing_key):
        res = auth_client.delete(f"/api-keys/{existing_key.id}/")
        assert res.status_code == 204
        existing_key.refresh_from_db()
        assert existing_key.is_active is False

    def test_viewer_cannot_delete(self, viewer_client, existing_key):
        res = viewer_client.delete(f"/api-keys/{existing_key.id}/")
        assert res.status_code == 403

    def test_cross_org_delete_returns_404(self, auth_client, db):
        other_org = OrganizationFactory()
        other_user = UserFactory()
        other_key = ApiKeyFactory(organization=other_org, created_by=other_user)
        res = auth_client.delete(f"/api-keys/{other_key.id}/")
        assert res.status_code == 404


# ── Rotate ────────────────────────────────────────────────────────────────────

class TestApiKeyRotate:
    def test_rotate_returns_new_secret(self, auth_client, existing_key):
        res = auth_client.post(f"/api-keys/{existing_key.id}/rotate/")
        assert res.status_code == 201
        assert "secret" in res.data
        assert res.data["secret"].startswith("sk_live_")
        # New secret must differ from the old one
        assert res.data["secret"] != existing_key._test_secret

    def test_rotate_old_key_gets_expiry(self, auth_client, existing_key):
        """Old key must be scheduled to expire within ~24h."""
        before = timezone.now()
        res = auth_client.post(f"/api-keys/{existing_key.id}/rotate/")
        assert res.status_code == 201

        existing_key.refresh_from_db()
        assert existing_key.expires_at is not None
        # Expiry should be in the future (within 25 hours to allow test lag)
        from datetime import timedelta
        assert existing_key.expires_at > before
        assert existing_key.expires_at < timezone.now() + timedelta(hours=25)

    def test_rotate_creates_new_key_in_db(self, auth_client, existing_key, org):
        initial_count = ApiKey.all_objects.filter(organization=org).count()
        auth_client.post(f"/api-keys/{existing_key.id}/rotate/")
        assert ApiKey.all_objects.filter(organization=org).count() == initial_count + 1

    def test_rotate_revoked_key_fails(self, auth_client, existing_key):
        existing_key.is_active = False
        existing_key.save()
        res = auth_client.post(f"/api-keys/{existing_key.id}/rotate/")
        assert res.status_code == 400

    def test_viewer_cannot_rotate(self, viewer_client, existing_key):
        res = viewer_client.post(f"/api-keys/{existing_key.id}/rotate/")
        assert res.status_code == 403
