"""
Custom JWT token serializer.

Embeds `org_id` and `role` directly into the JWT access token payload
so that every downstream middleware can resolve tenant context and RBAC
permissions without extra database round-trips.

How it works:
  1. User logs in with email + password.
  2. Django loads the User and their primary OrganizationMembership.
  3. We override `get_token()` to inject org_id + role into the claims.
  4. The signed JWT is returned to the client.

On subsequent requests:
  - The auth middleware decodes the JWT.
  - It reads org_id and role directly from the payload.
  - No extra DB query needed to know who the user is or what they can do.

If a user belongs to multiple organisations, the token represents one
organisation session.  Switching org requires a new token (re-login or
a separate /auth/switch-org endpoint — Phase 2+).
"""

from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.tenants.models import OrganizationMembership


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Embed basic user info
        token["email"] = user.email
        token["full_name"] = user.full_name

        # Resolve the user's primary membership
        # Phase 1: pick the first active membership.
        # Phase 2+: support org-switching via a dedicated endpoint.
        membership = (
            OrganizationMembership.objects.filter(user=user)
            .select_related("organization")
            .order_by("joined_at")
            .first()
        )

        if membership:
            token["org_id"] = str(membership.organization.id)
            token["org_slug"] = membership.organization.slug
            token["role"] = membership.role
        else:
            token["org_id"] = None
            token["org_slug"] = None
            token["role"] = None

        return token
