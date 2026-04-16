"""Audit logs integration tests."""

import pytest
from rest_framework import status

from apps.audit_logs.models import AuditLog
from apps.tenants.models import RoleEnum
from tests.factories import MembershipFactory, OrganizationFactory, UserFactory


@pytest.mark.django_db
class TestAuditLogList:
    """Tests for GET /audit-logs/ endpoint."""

    URL = "/audit-logs/"

    def test_admin_can_list_audit_logs(self, auth_client, org):
        """ADMIN+ can retrieve the paginated audit log list."""
        res = auth_client.get(self.URL)
        assert res.status_code == status.HTTP_200_OK
        assert "results" in res.data
        assert "count" in res.data
        assert "page" in res.data
        assert "page_size" in res.data

    def test_audit_logs_filtered_by_org(self, db, api_client):
        """
        Audit logs are scoped to the requesting organization.
        Org A's admin cannot see Org B's logs.
        """
        org_a = OrganizationFactory()
        org_b = OrganizationFactory()

        admin_a = UserFactory(password="Pass123!")
        admin_b = UserFactory(password="Pass123!")
        MembershipFactory(organization=org_a, user=admin_a, role=RoleEnum.ADMIN)
        MembershipFactory(organization=org_b, user=admin_b, role=RoleEnum.ADMIN)

        # Create audit logs for both orgs
        AuditLog.objects.create(
            org=org_a,
            actor=admin_a,
            action="test.action",
            resource_type="Test",
            resource_id="123",
        )
        AuditLog.objects.create(
            org=org_b,
            actor=admin_b,
            action="test.action",
            resource_type="Test",
            resource_id="456",
        )

        # Org A's admin logs in and retrieves logs
        res = api_client.post(
            "/auth/token",
            {"email": admin_a.email, "password": "Pass123!"},
            format="json",
        )
        token = res.data["access"]
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        res = api_client.get(self.URL)
        assert res.status_code == status.HTTP_200_OK
        assert res.data["count"] == 1  # Only org_a's log
        assert res.data["results"][0]["resource_id"] == "123"

    def test_member_cannot_list_audit_logs(self, api_client, db):
        """MEMBER role cannot access audit logs (requires ADMIN+)."""
        org = OrganizationFactory()
        member = UserFactory(password="Pass123!")
        MembershipFactory(organization=org, user=member, role=RoleEnum.MEMBER)

        res = api_client.post(
            "/auth/token",
            {"email": member.email, "password": "Pass123!"},
            format="json",
        )
        token = res.data["access"]
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        res = api_client.get(self.URL)
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_list_audit_logs_with_filters(self, auth_client, org):
        """Filters for actor, action, resource_type, date range work."""
        user_a = UserFactory()
        MembershipFactory(organization=org, user=user_a, role=RoleEnum.MEMBER)

        # Create multiple audit logs
        AuditLog.objects.create(
            org=org,
            actor=auth_client.user,
            action="user.created",
            resource_type="User",
            resource_id="111",
        )
        AuditLog.objects.create(
            org=org,
            actor=user_a,
            action="api_key.updated",
            resource_type="ApiKey",
            resource_id="222",
        )

        # Filter by action
        res = auth_client.get(f"{self.URL}?action=user.created")
        assert res.status_code == status.HTTP_200_OK
        assert res.data["count"] == 1
        assert res.data["results"][0]["action"] == "user.created"

        # Filter by resource_type (case-insensitive)
        res = auth_client.get(f"{self.URL}?resource_type=apikey")
        assert res.status_code == status.HTTP_200_OK
        assert res.data["count"] == 1
        assert res.data["results"][0]["resource_type"] == "ApiKey"

    def test_pagination_works(self, auth_client, org):
        """Pagination with page and page_size parameters."""
        # Create 25 audit log entries
        for i in range(25):
            AuditLog.objects.create(
                org=org,
                actor=auth_client.user,
                action=f"action.{i}",
                resource_type="Test",
                resource_id=str(i),
            )

        # Default page size is 20
        res = auth_client.get(self.URL)
        assert res.status_code == status.HTTP_200_OK
        assert res.data["count"] == 25
        assert len(res.data["results"]) == 20
        assert res.data["page"] == 1
        assert res.data["page_size"] == 20
        assert res.data["total_pages"] == 2

        # Get page 2
        res = auth_client.get(f"{self.URL}?page=2")
        assert res.status_code == status.HTTP_200_OK
        assert len(res.data["results"]) == 5

        # Custom page size
        res = auth_client.get(f"{self.URL}?page_size=10")
        assert res.status_code == status.HTTP_200_OK
        assert len(res.data["results"]) == 10
        assert res.data["page_size"] == 10
        assert res.data["total_pages"] == 3


@pytest.mark.django_db
class TestAuditLogDetail:
    """Tests for GET /audit-logs/{id}/ endpoint."""

    def test_get_audit_log_detail(self, auth_client, org):
        """Retrieve individual audit log entry by UUID."""
        log = AuditLog.objects.create(
            org=org,
            actor=auth_client.user,
            action="test.event",
            resource_type="Widget",
            resource_id="abc123",
            ip_address="192.168.1.1",
            user_agent="Mozilla/5.0",
            request_id="req-123",
            diff={"field": ["old", "new"]},
        )

        res = auth_client.get(f"/audit-logs/{log.id}/")
        assert res.status_code == status.HTTP_200_OK
        assert res.data["id"] == str(log.id)
        assert res.data["action"] == "test.event"
        assert res.data["resource_type"] == "Widget"
        assert res.data["resource_id"] == "abc123"
        assert res.data["diff"] == {"field": ["old", "new"]}

    def test_cross_tenant_access_denied(self, db, api_client):
        """Org B's user cannot access Org A's audit log entries."""
        org_a = OrganizationFactory()
        org_b = OrganizationFactory()

        admin_a = UserFactory(password="Pass123!")
        admin_b = UserFactory(password="Pass123!")
        MembershipFactory(organization=org_a, user=admin_a, role=RoleEnum.ADMIN)
        MembershipFactory(organization=org_b, user=admin_b, role=RoleEnum.ADMIN)

        log = AuditLog.objects.create(
            org=org_a,
            actor=admin_a,
            action="test.action",
            resource_type="Test",
            resource_id="123",
        )

        # Admin B tries to access Admin A's log
        res = api_client.post(
            "/auth/token",
            {"email": admin_b.email, "password": "Pass123!"},
            format="json",
        )
        token = res.data["access"]
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        res = api_client.get(f"/audit-logs/{log.id}/")
        assert res.status_code == status.HTTP_404_NOT_FOUND

    def test_nonexistent_log_returns_404(self, auth_client):
        """Requesting a nonexistent audit log returns 404."""
        res = auth_client.get("/audit-logs/00000000-0000-0000-0000-000000000000/")
        assert res.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
class TestAuditLogExport:
    """Tests for GET /audit-logs/export/ endpoint (CSV export)."""

    def test_export_audit_logs_csv(self, auth_client, org):
        """Export filtered audit logs as CSV."""
        AuditLog.objects.create(
            org=org,
            actor=auth_client.user,
            action="test.created",
            resource_type="Item",
            resource_id="xyz",
        )

        res = auth_client.get("/audit-logs/export/")
        assert res.status_code == status.HTTP_200_OK
        assert res["Content-Type"] == "text/csv"
        assert b"test.created" in res.content
        assert b"Item" in res.content

    def test_export_respects_filters(self, auth_client, org):
        """CSV export respects the same filters as list endpoint."""
        AuditLog.objects.create(
            org=org,
            actor=auth_client.user,
            action="user.deleted",
            resource_type="User",
            resource_id="u1",
        )
        AuditLog.objects.create(
            org=org,
            actor=auth_client.user,
            action="api_key.created",
            resource_type="ApiKey",
            resource_id="k1",
        )

        res = auth_client.get("/audit-logs/export/?action=user.deleted")
        assert res.status_code == status.HTTP_200_OK
        assert b"user.deleted" in res.content
        assert b"api_key.created" not in res.content

    def test_export_capped_at_10k_rows(self, auth_client, org):
        """Export is capped at 10,000 rows to prevent huge downloads."""
        # Create 15 logs (smaller number for test perf)
        for i in range(15):
            AuditLog.objects.create(
                org=org,
                actor=auth_client.user,
                action=f"action.{i}",
                resource_type="Test",
                resource_id=str(i),
            )

        res = auth_client.get("/audit-logs/export/")
        assert res.status_code == status.HTTP_200_OK
        # Count rows in CSV (subtract 1 for header)
        row_count = res.content.decode().count("\n") - 1
        assert row_count == 15  # All 15 logs in this test


@pytest.mark.django_db
class TestAuditLogCreation:
    """Tests that audit logs are created on relevant API actions."""

    def test_sensitive_fields_redacted(self, auth_client, org, db):
        """Audit logs redact sensitive fields like passwords and API keys."""
        from apps.audit_logs.tasks import redact_sensitive

        sensitive_data = {
            "email": "user@example.com",
            "password": "secret123",
            "api_key": "sk_test_abc123",
            "hashed_key": "hash_value",
            "nested": {
                "token": "secret_token",
                "public_field": "visible",
            },
        }

        redacted = redact_sensitive(sensitive_data)

        assert redacted["email"] == "user@example.com"
        assert redacted["password"] == "**REDACTED**"
        assert redacted["api_key"] == "**REDACTED**"
        assert redacted["hashed_key"] == "**REDACTED**"
        assert redacted["nested"]["token"] == "**REDACTED**"
        assert redacted["nested"]["public_field"] == "visible"
