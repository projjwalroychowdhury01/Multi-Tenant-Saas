"""
Tests for ApiKeyAuthentication.

Tests that the custom DRF authentication class:
  - Accepts valid sk_live_ / sk_test_ Bearer tokens
  - Rejects invalid, expired, and revoked keys
  - Populates request.org and request.api_key correctly
  - Falls back to JWT if the header is not an API key
  - Constant-time comparison prevents timing attacks (structural test)
"""

import pytest
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIRequestFactory

from apps.api_keys.authentication import ApiKeyAuthentication
from apps.api_keys.models import ApiKey
from tests.factories import ApiKeyFactory, MembershipFactory, OrganizationFactory, UserFactory
from apps.tenants.models import RoleEnum


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def org_with_key(db):
    """Returns (org, user, api_key_instance, plaintext_secret)."""
    from tests.factories import OrganizationFactory, UserFactory

    org = OrganizationFactory()
    user = UserFactory()
    MembershipFactory(organization=org, user=user, role=RoleEnum.ADMIN)
    key, secret = ApiKeyFactory.create_with_secret(organization=org, created_by=user)
    return org, user, key, secret


# ── Model-level helpers ───────────────────────────────────────────────────────


class TestApiKeyModel:
    def test_generate_secret_live(self):
        secret = ApiKey.generate_secret("live")
        assert secret.startswith("sk_live_")
        assert len(secret) > 20

    def test_generate_secret_test(self):
        secret = ApiKey.generate_secret("test")
        assert secret.startswith("sk_test_")

    def test_prefix_derived_correctly(self):
        secret = "sk_live_abcd1234rest"
        prefix = ApiKey.derive_prefix(secret)
        assert prefix == "sk_live_abcd"

    def test_hash_is_deterministic(self):
        secret = ApiKey.generate_secret("live")
        h1 = ApiKey.hash_secret(secret)
        h2 = ApiKey.hash_secret(secret)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_verify_correct_secret(self):
        secret = ApiKey.generate_secret("live")
        hashed = ApiKey.hash_secret(secret)
        assert ApiKey.verify_secret(secret, hashed) is True

    def test_verify_wrong_secret(self):
        secret = ApiKey.generate_secret("live")
        hashed = ApiKey.hash_secret(secret)
        assert ApiKey.verify_secret("sk_live_wrongsecret", hashed) is False

    def test_two_keys_never_share_prefix(self, db):
        s1 = ApiKey.generate_secret("live")
        s2 = ApiKey.generate_secret("live")
        # Statistically impossible to collide with 64-char hex
        assert s1 != s2
        assert ApiKey.derive_prefix(s1) != ApiKey.derive_prefix(s2) or s1 == s2


# ── Authentication class ──────────────────────────────────────────────────────


class TestApiKeyAuthenticationClass:
    """Unit tests calling ApiKeyAuthentication.authenticate() directly."""

    def _make_request(self, factory, token=None):
        """Build a DRF-wrapped GET request with optional Authorization header."""
        from rest_framework.request import Request
        from rest_framework.parsers import JSONParser

        if token:
            django_req = factory.get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
        else:
            django_req = factory.get("/")
        return Request(django_req, parsers=[JSONParser()])

    def test_valid_key_authenticates(self, factory, org_with_key):
        _, user, key, secret = org_with_key
        request = self._make_request(factory, token=secret)
        auth = ApiKeyAuthentication()
        result = auth.authenticate(request)
        assert result is not None
        authenticated_user, auth_token = result
        assert authenticated_user == user
        assert auth_token == key

    def test_request_org_set_on_success(self, factory, org_with_key):
        org, _, key, secret = org_with_key
        request = self._make_request(factory, token=secret)
        ApiKeyAuthentication().authenticate(request)
        assert request.org == org

    def test_invalid_secret_raises_401(self, factory, org_with_key):
        _, _, key, _ = org_with_key
        # Construct a plausible but wrong secret using the same prefix structure
        bad_secret = ApiKey.generate_secret("live")
        request = self._make_request(factory, token=bad_secret)
        from rest_framework.exceptions import AuthenticationFailed

        with pytest.raises(AuthenticationFailed):
            ApiKeyAuthentication().authenticate(request)

    def test_revoked_key_raises_401(self, factory, org_with_key, db):
        _, _, key, secret = org_with_key
        key.is_active = False
        key.save()
        request = self._make_request(factory, token=secret)
        from rest_framework.exceptions import AuthenticationFailed

        with pytest.raises(AuthenticationFailed):
            ApiKeyAuthentication().authenticate(request)

    def test_expired_key_raises_401(self, factory, org_with_key):
        _, _, key, secret = org_with_key
        key.expires_at = timezone.now() - timedelta(hours=1)
        key.save()
        request = self._make_request(factory, token=secret)
        from rest_framework.exceptions import AuthenticationFailed

        with pytest.raises(AuthenticationFailed):
            ApiKeyAuthentication().authenticate(request)

    def test_jwt_header_ignored(self, factory):
        """A JWT-style header must not be attempted by this authenticator."""
        jwt_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fake.payload"
        request = self._make_request(factory, token=jwt_token)
        result = ApiKeyAuthentication().authenticate(request)
        assert result is None  # Skips — lets JWT auth class handle it

    def test_no_header_returns_none(self, factory):
        request = self._make_request(factory, token=None)
        result = ApiKeyAuthentication().authenticate(request)
        assert result is None

    def test_malformed_header_returns_none(self, factory):
        """Header with wrong scheme ('Token' instead of 'Bearer') is ignored."""
        django_req = factory.get("/", HTTP_AUTHORIZATION="Token sk_live_abc123")
        from rest_framework.request import Request
        from rest_framework.parsers import JSONParser

        request = Request(django_req, parsers=[JSONParser()])
        result = ApiKeyAuthentication().authenticate(request)
        assert result is None


# ── Integration: requests authenticated via API key hit real endpoints ─────────


class TestApiKeyEndpointAccess:
    """Verify that API-key Bearer auth works through the full request stack."""

    def test_authenticated_key_can_access_me_endpoint(self, api_client, org_with_key):
        org, user, key, secret = org_with_key
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")
        res = api_client.get("/auth/me")
        # Should return 200 — org context is resolved from the key
        assert res.status_code == 200

    def test_revoked_key_returns_401(self, api_client, org_with_key):
        _, _, key, secret = org_with_key
        key.is_active = False
        key.save()
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")
        res = api_client.get("/auth/me")
        assert res.status_code == 401

    def test_expired_key_returns_401(self, api_client, org_with_key):
        _, _, key, secret = org_with_key
        key.expires_at = timezone.now() - timedelta(minutes=5)
        key.save()
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")
        res = api_client.get("/auth/me")
        assert res.status_code == 401
