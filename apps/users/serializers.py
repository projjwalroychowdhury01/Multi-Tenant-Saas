"""
User and auth serializers.

RegisterSerializer       — validate + create user + org atomically
UserSerializer           — read-only representation of the current user
InviteSerializer         — validate invitation request (email + role)
AcceptInviteSerializer   — validate invite token + optional password
"""

import hmac
import hashlib
import base64
import json
from datetime import datetime, timezone, timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.utils.text import slugify
from rest_framework import serializers

from apps.tenants.models import Organization, OrganizationMembership, RoleEnum
from apps.users.models import User


# ── Register ─────────────────────────────────────────────────────────────────


class RegisterSerializer(serializers.Serializer):
    """
    Creates a User and a new Organization in a single atomic transaction.
    The registering user is assigned the OWNER role automatically.
    """

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    full_name = serializers.CharField(max_length=255, required=False, default="")
    org_name = serializers.CharField(max_length=255)

    def validate_email(self, value):
        if User.objects.filter(email=value.lower()).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate_org_name(self, value):
        slug = slugify(value)
        if Organization.all_objects.filter(slug=slug).exists():
            raise serializers.ValidationError(
                "An organisation with a similar name already exists. "
                "Please choose a different name."
            )
        return value

    def create(self, validated_data):
        from django.db import transaction

        with transaction.atomic():
            # 1. Create the user
            user = User.objects.create_user(
                email=validated_data["email"],
                password=validated_data["password"],
                full_name=validated_data.get("full_name", ""),
            )

            # 2. Create the organisation
            org = Organization.all_objects.create(
                name=validated_data["org_name"],
                slug=slugify(validated_data["org_name"]),
            )

            # 3. Assign OWNER membership
            OrganizationMembership.objects.create(
                organization=org,
                user=user,
                role=RoleEnum.OWNER,
            )

        return user, org


# ── User ──────────────────────────────────────────────────────────────────────


class UserSerializer(serializers.ModelSerializer):
    """Read-only serializer for the /me endpoint."""

    org_id = serializers.SerializerMethodField()
    org_slug = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "is_verified",
            "org_id",
            "org_slug",
            "role",
            "last_login",
            "created_at",
        ]
        read_only_fields = fields

    def _get_membership(self, obj):
        # request.org set by auth middleware
        request = self.context.get("request")
        org = getattr(request, "org", None) if request else None
        if org:
            return OrganizationMembership.objects.filter(
                user=obj, organization=org
            ).first()
        return OrganizationMembership.objects.filter(user=obj).order_by("joined_at").first()

    def get_org_id(self, obj):
        m = self._get_membership(obj)
        return str(m.organization_id) if m else None

    def get_org_slug(self, obj):
        m = self._get_membership(obj)
        return m.organization.slug if m else None

    def get_role(self, obj):
        m = self._get_membership(obj)
        return m.role if m else None


# ── Invite ────────────────────────────────────────────────────────────────────


def _make_invite_token(org_id: str, email: str, role: str) -> str:
    """
    Create a signed, base64url-encoded invitation token.

    Token payload: {"org_id": ..., "email": ..., "role": ..., "exp": unix_ts}
    The HMAC-SHA256 signature uses settings.SECRET_KEY as the key.

    The token is single-use: it is stored in Redis after creation and
    deleted on acceptance.  Expiry defaults to INVITE_TOKEN_EXPIRY_HOURS.
    """
    expiry_hours = getattr(settings, "INVITE_TOKEN_EXPIRY_HOURS", 48)
    exp = datetime.now(tz=timezone.utc) + timedelta(hours=expiry_hours)
    payload = {
        "org_id": org_id,
        "email": email,
        "role": role,
        "exp": exp.timestamp(),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode()

    sig = hmac.new(
        settings.SECRET_KEY.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()

    token = f"{payload_b64}.{sig}"

    # Store in Redis so we can mark it as used on acceptance
    cache_key = f"invite:{sig}"
    cache.set(cache_key, payload_b64, timeout=int(expiry_hours * 3600))

    return token


def _validate_invite_token(token: str) -> dict:
    """
    Validate a signed invitation token.

    Raises serializers.ValidationError for:
      - Malformed token
      - Invalid signature (HMAC mismatch)
      - Expired token
      - Already-used token (not in Redis)
    """
    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        raise serializers.ValidationError("Invalid invitation token format.")

    # 1. Verify HMAC
    expected_sig = hmac.new(
        settings.SECRET_KEY.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        raise serializers.ValidationError("Invalid or tampered invitation token.")

    # 2. Check single-use via Redis
    cache_key = f"invite:{sig}"
    if not cache.get(cache_key):
        raise serializers.ValidationError(
            "Invitation token has expired or has already been used."
        )

    # 3. Decode payload
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    except Exception:
        raise serializers.ValidationError("Could not decode invitation token.")

    # 4. Check expiry
    if datetime.now(tz=timezone.utc).timestamp() > payload["exp"]:
        raise serializers.ValidationError("Invitation token has expired.")

    return payload, cache_key


class InviteSerializer(serializers.Serializer):
    """Validates an invitation request and returns a signed invite token."""

    email = serializers.EmailField()
    role = serializers.ChoiceField(
        choices=[r for r in RoleEnum.values if r != RoleEnum.OWNER],
        default=RoleEnum.MEMBER,
    )

    def validate_email(self, value):
        return value.lower()

    def create(self, validated_data):
        # org is injected by the view from request.org
        org = self.context["org"]
        token = _make_invite_token(
            org_id=str(org.id),
            email=validated_data["email"],
            role=validated_data["role"],
        )
        return {"token": token, "email": validated_data["email"], "role": validated_data["role"]}


class AcceptInviteSerializer(serializers.Serializer):
    """
    Validates an invitation token from the URL and creates/logs-in the invited user.
    """

    token = serializers.CharField()
    # Password is only required if the user is new (no existing account)
    password = serializers.CharField(write_only=True, required=False, min_length=8)
    full_name = serializers.CharField(max_length=255, required=False, default="")

    def validate(self, attrs):
        payload, cache_key = _validate_invite_token(attrs["token"])
        attrs["_payload"] = payload
        attrs["_cache_key"] = cache_key
        return attrs

    def create(self, validated_data):
        from django.db import transaction

        payload = validated_data["_payload"]
        cache_key = validated_data["_cache_key"]
        email = payload["email"]
        org_id = payload["org_id"]
        role = payload["role"]

        with transaction.atomic():
            # Get or create the user
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "full_name": validated_data.get("full_name", ""),
                    "is_verified": True,  # Email validated by invitation
                },
            )
            if created:
                password = validated_data.get("password")
                if not password:
                    raise serializers.ValidationError(
                        {"password": "A password is required for new accounts."}
                    )
                user.set_password(password)
                user.full_name = validated_data.get("full_name", "")
                user.save()

            # Create membership (or update role if re-invited)
            try:
                org = Organization.all_objects.get(id=org_id)
            except Organization.DoesNotExist:
                raise serializers.ValidationError("The invited organisation no longer exists.")

            OrganizationMembership.objects.update_or_create(
                organization=org,
                user=user,
                defaults={"role": role},
            )

            # Mark the token as used by deleting from Redis
            cache.delete(cache_key)

        return user
