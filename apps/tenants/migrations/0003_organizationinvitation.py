# Generated migration for Phase 3: OrganizationInvitation model

import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0002_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InvitationStatus",
            fields=[],
            options={
                "proxy": False,
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="OrganizationInvitation",
            fields=[
                # Timestamps from TimeStampedModel
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                # Primary key
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                # Target email
                ("email", models.EmailField(db_index=True, max_length=254)),
                # Role offered
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("OWNER", "Owner"),
                            ("ADMIN", "Admin"),
                            ("MEMBER", "Member"),
                            ("VIEWER", "Viewer"),
                            ("BILLING", "Billing"),
                        ],
                        default="MEMBER",
                        max_length=20,
                    ),
                ),
                # Secure token
                (
                    "token",
                    models.CharField(
                        db_index=True,
                        editable=False,
                        max_length=64,
                        unique=True,
                    ),
                ),
                # Lifecycle state
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("ACCEPTED", "Accepted"),
                            ("EXPIRED", "Expired"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                # FK: which org
                (
                    "organization",
                    models.ForeignKey(
                        db_index=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invitations",
                        to="tenants.organization",
                    ),
                ),
                # FK: who invited
                (
                    "invited_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="sent_org_invitations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Organization Invitation",
                "verbose_name_plural": "Organization Invitations",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="organizationinvitation",
            index=models.Index(fields=["token"], name="tenants_org_token_inv_idx"),
        ),
        migrations.AddIndex(
            model_name="organizationinvitation",
            index=models.Index(
                fields=["organization", "email"],
                name="tenants_org_email_inv_idx",
            ),
        ),
    ]
