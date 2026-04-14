"""
Phase 3 — Invitation & Onboarding test suite.

Coverage
────────
  Token generation
    [ ] Tokens are URL-safe strings of the expected length
    [ ] Each invitation gets a unique token

  CreateInvitation validation
    [ ] OWNER cannot invite to OWNER role
    [ ] ADMIN cannot invite to ADMIN rank or above
    [ ] Cannot invite an already-active member
    [ ] Cannot create a duplicate PENDING invite for the same email
    [ ] MEMBER/VIEWER cannot create invitations (no users:invite permission)

  ListInvitations
    [ ] OWNER/ADMIN can list invitations
    [ ] MEMBER/VIEWER can list (users:read) — read only
    [ ] Non-member gets 404

  RevokeInvitation
    [ ] ADMIN can revoke a PENDING invitation → status becomes EXPIRED
    [ ] Cannot revoke an ACCEPTED invitation
    [ ] MEMBER cannot revoke — 403
    [ ] Cannot revoke an invitation from another org

  Token resolution (GET /invitations/<token>/)
    [ ] Valid PENDING token returns org + inviter metadata
    [ ] Invalid token returns 404
    [ ] EXPIRED/ACCEPTED token returns 410 Gone

  Accept flow (POST /invitations/<token>/accept/)
    [ ] Happy path: user accepts, membership created, status ACCEPTED
    [ ] Email mismatch returns 403
    [ ] Already-member returns 409
    [ ] Expired token returns 410
    [ ] Unauthenticated accept returns 401
"""

import pytest
from rest_framework.test import APIClient

from apps.tenants.models import (
    InvitationStatus,
    OrganizationInvitation,
    OrganizationMembership,
    RoleEnum,
)
from tests.factories import MembershipFactory, OrganizationFactory, UserFactory


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def org(db):
    return OrganizationFactory()


@pytest.fixture
def other_org(db):
    return OrganizationFactory()


@pytest.fixture
def owner(db, org):
    u = UserFactory(password="TestPass123!")
    MembershipFactory(organization=org, user=u, role=RoleEnum.OWNER)
    return u


@pytest.fixture
def admin_user(db, org):
    u = UserFactory(password="TestPass123!")
    MembershipFactory(organization=org, user=u, role=RoleEnum.ADMIN)
    return u


@pytest.fixture
def member_user(db, org):
    u = UserFactory(password="TestPass123!")
    MembershipFactory(organization=org, user=u, role=RoleEnum.MEMBER)
    return u


@pytest.fixture
def viewer_user(db, org):
    u = UserFactory(password="TestPass123!")
    MembershipFactory(organization=org, user=u, role=RoleEnum.VIEWER)
    return u


def _get_token(api_client, user):
    """Authenticate and return a JWT-authenticated APIClient."""
    res = api_client.post(
        "/auth/token",
        {"email": user.email, "password": "TestPass123!"},
        format="json",
    )
    assert res.status_code == 200, f"Login failed: {res.data}"
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
    return api_client


@pytest.fixture
def owner_client(db, org, owner):
    client = APIClient()
    return _get_token(client, owner)


@pytest.fixture
def admin_client(db, org, admin_user):
    client = APIClient()
    return _get_token(client, admin_user)


@pytest.fixture
def member_client(db, org, member_user):
    client = APIClient()
    return _get_token(client, member_user)


@pytest.fixture
def viewer_client(db, org, viewer_user):
    client = APIClient()
    return _get_token(client, viewer_user)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _invite_url(org):
    return f"/orgs/{org.id}/invitations/"


def _revoke_url(org, inv):
    return f"/orgs/{org.id}/invitations/{inv.id}/"


def _resolve_url(token):
    return f"/invitations/{token}/"


def _accept_url(token):
    return f"/invitations/{token}/accept/"


# ── Token Generation ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestTokenGeneration:
    def test_token_is_generated_on_save(self, org, owner):
        inv = OrganizationInvitation.objects.create(
            organization=org,
            email="newuser@example.com",
            role=RoleEnum.MEMBER,
            invited_by=owner,
        )
        assert inv.token
        assert len(inv.token) >= 40  # urlsafe_b64 of 32 bytes = 43 chars

    def test_tokens_are_unique(self, org, owner):
        tokens = set()
        for i in range(10):
            inv = OrganizationInvitation.objects.create(
                organization=org,
                email=f"unique{i}@example.com",
                role=RoleEnum.MEMBER,
                invited_by=owner,
            )
            tokens.add(inv.token)
        assert len(tokens) == 10


# ── Create Invitation ─────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestCreateInvitation:
    def test_owner_can_invite_member(self, org, owner_client):
        res = owner_client.post(
            _invite_url(org),
            {"email": "alice@example.com", "role": RoleEnum.MEMBER},
            format="json",
        )
        assert res.status_code == 201
        assert res.data["email"] == "alice@example.com"
        assert res.data["role"] == RoleEnum.MEMBER
        assert res.data["status"] == InvitationStatus.PENDING

    def test_admin_can_invite_member(self, org, admin_client):
        res = admin_client.post(
            _invite_url(org),
            {"email": "bob@example.com", "role": RoleEnum.MEMBER},
            format="json",
        )
        assert res.status_code == 201

    def test_cannot_invite_to_owner_role(self, org, owner_client):
        res = owner_client.post(
            _invite_url(org),
            {"email": "evil@example.com", "role": RoleEnum.OWNER},
            format="json",
        )
        assert res.status_code == 400

    def test_admin_cannot_invite_to_admin_rank(self, org, admin_client):
        """ADMIN cannot elevate an invite to ADMIN or OWNER."""
        res = admin_client.post(
            _invite_url(org),
            {"email": "sneaky@example.com", "role": RoleEnum.ADMIN},
            format="json",
        )
        assert res.status_code == 403
        assert res.data["code"] == "rank_violation"

    def test_cannot_invite_existing_member(self, org, owner_client, member_user):
        """Inviting someone who is already a member must fail."""
        res = owner_client.post(
            _invite_url(org),
            {"email": member_user.email, "role": RoleEnum.VIEWER},
            format="json",
        )
        assert res.status_code == 400

    def test_duplicate_pending_invite_rejected(self, org, owner_client):
        """Second invite to same email in same org must be rejected."""
        email = "dup@example.com"
        r1 = owner_client.post(
            _invite_url(org),
            {"email": email, "role": RoleEnum.MEMBER},
            format="json",
        )
        assert r1.status_code == 201

        r2 = owner_client.post(
            _invite_url(org),
            {"email": email, "role": RoleEnum.MEMBER},
            format="json",
        )
        assert r2.status_code == 400

    def test_member_cannot_invite(self, org, member_client):
        """MEMBER lacks users:invite permission."""
        res = member_client.post(
            _invite_url(org),
            {"email": "nope@example.com", "role": RoleEnum.MEMBER},
            format="json",
        )
        assert res.status_code == 403

    def test_viewer_cannot_invite(self, org, viewer_client):
        res = viewer_client.post(
            _invite_url(org),
            {"email": "nope@example.com", "role": RoleEnum.VIEWER},
            format="json",
        )
        assert res.status_code == 403

    def test_unauthenticated_cannot_invite(self, org):
        client = APIClient()
        res = client.post(
            _invite_url(org),
            {"email": "anon@example.com", "role": RoleEnum.MEMBER},
            format="json",
        )
        assert res.status_code == 401


# ── List Invitations ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestListInvitations:
    def _create_invite(self, org, owner):
        return OrganizationInvitation.objects.create(
            organization=org,
            email="listtest@example.com",
            role=RoleEnum.MEMBER,
            invited_by=owner,
        )

    def test_owner_can_list(self, org, owner, owner_client):
        self._create_invite(org, owner)
        res = owner_client.get(_invite_url(org))
        assert res.status_code == 200
        assert res.data["count"] == 1

    def test_admin_can_list(self, org, owner, admin_client):
        self._create_invite(org, owner)
        res = admin_client.get(_invite_url(org))
        assert res.status_code == 200

    def test_member_can_list(self, org, owner, member_client):
        """MEMBER has users:read so they can see the invite list."""
        self._create_invite(org, owner)
        res = member_client.get(_invite_url(org))
        assert res.status_code == 200

    def test_non_member_gets_404(self, db, org):
        stranger = UserFactory(password="TestPass123!")
        client = APIClient()
        _get_token(client, stranger)
        res = client.get(_invite_url(org))
        assert res.status_code == 404


# ── Revoke Invitation ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestRevokeInvitation:
    def _make_pending(self, org, owner):
        return OrganizationInvitation.objects.create(
            organization=org,
            email="revoke@example.com",
            role=RoleEnum.MEMBER,
            invited_by=owner,
        )

    def test_admin_can_revoke_pending(self, org, owner, admin_client):
        inv = self._make_pending(org, owner)
        res = admin_client.delete(_revoke_url(org, inv))
        assert res.status_code == 204
        inv.refresh_from_db()
        assert inv.status == InvitationStatus.EXPIRED

    def test_owner_can_revoke_pending(self, org, owner, owner_client):
        inv = self._make_pending(org, owner)
        res = owner_client.delete(_revoke_url(org, inv))
        assert res.status_code == 204

    def test_cannot_revoke_accepted_invite(self, org, owner, admin_client):
        inv = self._make_pending(org, owner)
        inv.status = InvitationStatus.ACCEPTED
        inv.save()
        res = admin_client.delete(_revoke_url(org, inv))
        assert res.status_code == 400
        assert res.data["code"] == "invalid_state"

    def test_member_cannot_revoke(self, org, owner, member_client):
        inv = self._make_pending(org, owner)
        res = member_client.delete(_revoke_url(org, inv))
        assert res.status_code == 403

    def test_cannot_revoke_foreign_org_invite(self, db, org, other_org, owner_client):
        """OWNER of org A cannot revoke an invite that belongs to org B."""
        other_owner = UserFactory(password="TestPass123!")
        MembershipFactory(organization=other_org, user=other_owner, role=RoleEnum.OWNER)
        inv = OrganizationInvitation.objects.create(
            organization=other_org,
            email="foreign@example.com",
            role=RoleEnum.MEMBER,
            invited_by=other_owner,
        )
        # Try to delete from org (not other_org)
        url = f"/orgs/{org.id}/invitations/{inv.id}/"
        res = owner_client.delete(url)
        assert res.status_code in (403, 404)


# ── Token Resolution ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestPublicTokenResolution:
    def test_valid_token_returns_metadata(self, db, org, owner):
        inv = OrganizationInvitation.objects.create(
            organization=org,
            email="check@example.com",
            role=RoleEnum.MEMBER,
            invited_by=owner,
        )
        client = APIClient()
        res = client.get(_resolve_url(inv.token))
        assert res.status_code == 200
        assert res.data["org_name"] == org.name
        assert res.data["email"] == "check@example.com"
        assert "token" not in res.data  # token must not leak in this endpoint

    def test_invalid_token_returns_404(self, db):
        client = APIClient()
        res = client.get(_resolve_url("totally-invalid-token-xyz"))
        assert res.status_code == 404

    def test_expired_token_returns_410(self, db, org, owner):
        inv = OrganizationInvitation.objects.create(
            organization=org,
            email="expired@example.com",
            role=RoleEnum.MEMBER,
            invited_by=owner,
        )
        inv.status = InvitationStatus.EXPIRED
        inv.save()
        client = APIClient()
        res = client.get(_resolve_url(inv.token))
        assert res.status_code == 410

    def test_accepted_token_returns_410(self, db, org, owner):
        inv = OrganizationInvitation.objects.create(
            organization=org,
            email="done@example.com",
            role=RoleEnum.MEMBER,
            invited_by=owner,
        )
        inv.status = InvitationStatus.ACCEPTED
        inv.save()
        client = APIClient()
        res = client.get(_resolve_url(inv.token))
        assert res.status_code == 410


# ── Accept Flow ───────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAcceptInvitation:
    def _make_invite(self, org, owner, email="invited@example.com", role=RoleEnum.MEMBER):
        return OrganizationInvitation.objects.create(
            organization=org,
            email=email,
            role=role,
            invited_by=owner,
        )

    def test_happy_path_creates_membership(self, db, org, owner):
        """Invited user accepts → membership created, status ACCEPTED."""
        email = "happypath@example.com"
        invitee = UserFactory(email=email, password="TestPass123!")
        inv = self._make_invite(org, owner, email=email, role=RoleEnum.ADMIN)

        client = APIClient()
        _get_token(client, invitee)
        res = client.post(_accept_url(inv.token), format="json")

        assert res.status_code == 200
        assert res.data["role"] == RoleEnum.ADMIN

        # Membership must now exist
        assert OrganizationMembership.objects.filter(
            organization=org, user=invitee, role=RoleEnum.ADMIN
        ).exists()

        # Invitation must be ACCEPTED
        inv.refresh_from_db()
        assert inv.status == InvitationStatus.ACCEPTED

    def test_email_mismatch_is_403(self, db, org, owner):
        """User with a different email cannot accept someone else's invite."""
        inv = self._make_invite(org, owner, email="target@example.com")
        impostor = UserFactory(email="impostor@example.com", password="TestPass123!")

        client = APIClient()
        _get_token(client, impostor)
        res = client.post(_accept_url(inv.token), format="json")

        assert res.status_code == 403
        assert res.data["code"] == "email_mismatch"

    def test_already_member_returns_409(self, db, org, owner, member_user):
        """User who is already a member cannot accept an invite."""
        inv = self._make_invite(org, owner, email=member_user.email)

        client = APIClient()
        _get_token(client, member_user)
        res = client.post(_accept_url(inv.token), format="json")

        assert res.status_code == 409
        assert res.data["code"] == "already_member"

    def test_expired_token_returns_410_on_accept(self, db, org, owner):
        email = "expirytest@example.com"
        invitee = UserFactory(email=email, password="TestPass123!")
        inv = self._make_invite(org, owner, email=email)
        inv.status = InvitationStatus.EXPIRED
        inv.save()

        client = APIClient()
        _get_token(client, invitee)
        res = client.post(_accept_url(inv.token), format="json")
        assert res.status_code == 410

    def test_unauthenticated_accept_returns_401(self, db, org, owner):
        inv = self._make_invite(org, owner)
        client = APIClient()
        res = client.post(_accept_url(inv.token), format="json")
        assert res.status_code == 401
