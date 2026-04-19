"""
Auth views — all endpoints under /auth/

POST /auth/register          — create user + org + OWNER membership (atomic)
POST /auth/token             — login → JWT access + refresh pair
POST /auth/token/refresh     — exchange refresh token for new access token
POST /auth/logout            — blacklist the refresh token
POST /auth/invite            — ADMIN+ sends an invitation link
POST /auth/accept-invite/    — invited user joins the org
GET  /auth/me                — current user profile + org context
GET  /auth/me/permissions    — list permission scopes for current role
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.users.serializers import (
    AcceptInviteSerializer,
    InviteSerializer,
    RegisterSerializer,
    UserSerializer,
)
from apps.users.tokens import CustomTokenObtainPairSerializer

# ── Register ─────────────────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    """
    Create a new user and a new organization in a single atomic transaction.
    Returns JWT tokens so the user is immediately logged in.
    """
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user, org = serializer.save()

    # Embed org context into the token payload through our custom serializer.
    token = CustomTokenObtainPairSerializer.get_token(user)

    return Response(
        {
            "message": f"Welcome! Your organisation '{org.name}' has been created.",
            "user": {
                "id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
            },
            "org": {
                "id": str(org.id),
                "name": org.name,
                "slug": org.slug,
            },
            "tokens": {
                "access": str(token.access_token),
                "refresh": str(token),
            },
        },
        status=status.HTTP_201_CREATED,
    )


# ── Login ─────────────────────────────────────────────────────────────────────


class LoginView(TokenObtainPairView):
    """
    POST /auth/token

    Standard simplejwt login view but using our custom serializer
    that embeds org_id and role in the token payload.
    """

    serializer_class = CustomTokenObtainPairSerializer


# ── Logout ────────────────────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    """
    Blacklist the provided refresh token.
    The client should discard both the access and refresh tokens after this.
    """
    refresh_token = request.data.get("refresh")
    if not refresh_token:
        return Response(
            {"error": "Refresh token is required.", "code": "missing_token"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        token = RefreshToken(refresh_token)
        token.blacklist()
    except TokenError as e:
        return Response(
            {"error": str(e), "code": "token_error"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response({"message": "Successfully logged out."}, status=status.HTTP_200_OK)


# ── Me ────────────────────────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    """Return the currently authenticated user's profile and org context."""
    serializer = UserSerializer(request.user, context={"request": request})
    return Response(serializer.data)


# ── Me Permissions ────────────────────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me_permissions(request):
    """
    GET /auth/me/permissions

    Returns the list of permission scope strings granted to the current user
    based on their role in the active organisation.

    Response shape::

        {
            "org_id": "<uuid>",
            "role": "ADMIN",
            "permissions": ["users:read", "users:invite", ...]
        }

    If the user has no org context (e.g., no membership yet), the permissions
    list will be empty.
    """
    from apps.rbac.permissions import _get_request_role
    from apps.rbac.registry import get_role_permissions

    org = getattr(request, "org", None)
    role = _get_request_role(request)
    permissions = sorted(get_role_permissions(role)) if role else []

    return Response(
        {
            "org_id": str(org.id) if org else None,
            "role": role,
            "permissions": permissions,
        }
    )


# ── Invite ────────────────────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def invite(request):
    """
    POST /auth/invite

    ADMIN+ sends an invitation to the given email address.
    Returns the signed invite token (in production this would be emailed).

    Phase 1: returns the token directly in the response for testability.
    Phase 6: Celery email task will send the invite link by email.
    """
    org = getattr(request, "org", None)
    if org is None:
        return Response(
            {"error": "No active organisation context.", "code": "no_org"},
            status=status.HTTP_403_FORBIDDEN,
        )

    serializer = InviteSerializer(data=request.data, context={"org": org, "request": request})
    serializer.is_valid(raise_exception=True)
    result = serializer.save()

    return Response(
        {
            "message": f"Invitation created for {result['email']}.",
            "role": result["role"],
            # In production: email the link, not the raw token
            "invite_token": result["token"],
        },
        status=status.HTTP_200_OK,
    )


# ── Accept Invite ─────────────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
def accept_invite(request):
    """
    POST /auth/accept-invite/

    Validates the invite token, creates or updates the user, and
    creates the OrganizationMembership with the role from the token.
    Returns JWT tokens so the user is immediately logged in.
    """
    serializer = AcceptInviteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()

    token = CustomTokenObtainPairSerializer.get_token(user)

    return Response(
        {
            "message": "You have successfully joined the organisation.",
            "user": {
                "id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
            },
            "tokens": {
                "access": str(token.access_token),
                "refresh": str(token),
            },
        },
        status=status.HTTP_200_OK,
    )
