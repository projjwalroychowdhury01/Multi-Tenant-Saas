"""
Auth URL patterns — all mounted at /auth/ in the root urls.py.

POST /auth/register          — create user + org
POST /auth/token             — login → JWT
POST /auth/token/refresh     — refresh access token
POST /auth/logout            — blacklist refresh token
GET  /auth/me                — current user profile
GET  /auth/me/permissions    — permission scopes for current role
POST /auth/invite            — send org invitation
POST /auth/accept-invite/    — accept invitation and join org
"""

from django.urls import path

from rest_framework_simplejwt.views import TokenRefreshView

from apps.users.views import LoginView, accept_invite, invite, logout, me, me_permissions, register

urlpatterns = [
    path("register", register, name="auth-register"),
    path("token", LoginView.as_view(), name="auth-token"),
    path("token/refresh", TokenRefreshView.as_view(), name="auth-token-refresh"),
    path("logout", logout, name="auth-logout"),
    path("me", me, name="auth-me"),
    path("me/permissions", me_permissions, name="auth-me-permissions"),
    path("invite", invite, name="auth-invite"),
    path("accept-invite/", accept_invite, name="auth-accept-invite"),
]
