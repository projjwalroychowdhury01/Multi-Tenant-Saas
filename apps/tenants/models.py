"""
Tenant models.

Organization  — the top-level unit of tenancy (a company / workspace).
TenantModel   — abstract base that every tenant-scoped model inherits.
RoleEnum      — the five-tier role hierarchy.
OrganizationMembership — links a user to an org with a role.
"""

import uuid

from django.db import models
from django.utils.text import slugify

from apps.core.mixins import SoftDeleteMixin, TimeStampedModel
from apps.tenants.managers import TenantManager


# ── Role Hierarchy ─────────────────────────────────────────────────────────────


class RoleEnum(models.TextChoices):
    """
    Five-tier role hierarchy (highest → lowest authority):
      OWNER   — full control, including billing and org deletion
      ADMIN   — manage users, roles, API keys, settings
      MEMBER  — read + write own resources
      VIEWER  — read-only across the tenant
      BILLING — billing pages only
    """

    OWNER = "OWNER", "Owner"
    ADMIN = "ADMIN", "Admin"
    MEMBER = "MEMBER", "Member"
    VIEWER = "VIEWER", "Viewer"
    BILLING = "BILLING", "Billing"


# ── Organization ───────────────────────────────────────────────────────────────


class Organization(SoftDeleteMixin, TimeStampedModel):
    """
    Tenant root entity.

    - Every other tenant-scoped record holds a FK to this model.
    - `slug` is the stable, URL-safe public identifier.
    - `plan` will be a FK to billing.Plan once Phase 4 is built;
      for Phase 1 it is stored as a plain char field.
    - `stripe_customer_id` is null until the org subscribes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True, db_index=True)
    # Phase 1 — plan stored as text; will become a FK in Phase 4
    plan = models.CharField(max_length=50, default="FREE")
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Organization"
        verbose_name_plural = "Organizations"

    def __str__(self):
        return f"{self.name} ({self.slug})"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


# ── TenantModel (abstract base) ────────────────────────────────────────────────


class TenantModel(SoftDeleteMixin, TimeStampedModel):
    """
    Abstract base class for every tenant-scoped model.

    Provides:
      - `organization` FK — every row knows which tenant owns it
      - `objects`     — TenantManager (auto-filters by current org)
      - `all_objects` — plain models.Manager (bypass filter — admin/migration use)
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True


# ── OrganizationMembership ─────────────────────────────────────────────────────


class OrganizationMembership(TimeStampedModel):
    """
    Pivot table joining User ↔ Organization with a role.

    - A user may belong to multiple organisations with different roles.
    - A unique constraint on (organization, user) ensures one membership per pair.
    - `invited_by` records which user issued the invitation (null for org founders).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
        db_index=True,
    )
    user = models.ForeignKey(
        "users.User",
        on_delete=models.CASCADE,
        related_name="memberships",
        db_index=True,
    )
    role = models.CharField(max_length=20, choices=RoleEnum.choices, default=RoleEnum.MEMBER)
    invited_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_invitations",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("organization", "user")]
        indexes = [
            models.Index(fields=["organization", "user"]),
            models.Index(fields=["user", "organization"]),
        ]
        verbose_name = "Organization Membership"
        verbose_name_plural = "Organization Memberships"

    def __str__(self):
        return f"{self.user} — {self.organization} ({self.role})"
