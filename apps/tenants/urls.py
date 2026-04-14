"""
URL patterns for the tenants app — mounted at /orgs/ in root urls.py.

Member management:
  GET    /orgs/<org_id>/members/           — list members
  PATCH  /orgs/<org_id>/members/<uid>/     — change role
  DELETE /orgs/<org_id>/members/<uid>/     — remove member

Invitations (tenant-scoped):
  GET    /orgs/<org_id>/invitations/       — list invitations
  POST   /orgs/<org_id>/invitations/       — create invitation
  DELETE /orgs/<org_id>/invitations/<inv_id>/ — revoke invitation
"""

from django.urls import path

from apps.tenants import views

urlpatterns = [
    # ── Members ────────────────────────────────────────────────────
    path(
        "<uuid:org_id>/members/",
        views.list_members,
        name="org-members-list",
    ),
    path(
        "<uuid:org_id>/members/<uuid:uid>/role/",
        views.change_member_role,
        name="org-member-change-role",
    ),
    path(
        "<uuid:org_id>/members/<uuid:uid>/",
        views.remove_member,
        name="org-member-remove",
    ),
    # ── Invitations (Phase 3) ────────────────────────────────────
    path(
        "<uuid:org_id>/invitations/",
        views.list_or_create_invitations,
        name="org-invitations-list-create",
    ),
    path(
        "<uuid:org_id>/invitations/<uuid:inv_id>/",
        views.revoke_invitation,
        name="org-invitation-revoke",
    ),
]
