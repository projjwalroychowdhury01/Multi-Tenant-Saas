"""
URL patterns for the tenants app — mounted at /orgs/ in root urls.py.

GET    /orgs/<org_id>/members/           — list members
PATCH  /orgs/<org_id>/members/<uid>/     — change role
DELETE /orgs/<org_id>/members/<uid>/     — remove member
"""

from django.urls import path

from apps.tenants.views import change_member_role, list_members, remove_member

urlpatterns = [
    # List all members in an org
    path(
        "<uuid:org_id>/members/",
        list_members,
        name="org-members-list",
    ),
    # Change a specific member's role
    path(
        "<uuid:org_id>/members/<uuid:uid>/role/",
        change_member_role,
        name="org-member-change-role",
    ),
    # Remove a specific member from the org
    path(
        "<uuid:org_id>/members/<uuid:uid>/",
        remove_member,
        name="org-member-remove",
    ),
]
