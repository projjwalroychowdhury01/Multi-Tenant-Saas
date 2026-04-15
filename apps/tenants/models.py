"""
Tenant models.

Organization  — the top-level unit of tenancy (a company / workspace).
TenantModel   — abstract base that every tenant-scoped model inherits.
RoleEnum      — the five-tier role hierarchy.
OrganizationMembership — links a user to an org with a role.
OrganizationInvitation — secure email-based invitation to join an org.
"""

import secrets
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
    - `billing_plan` is a nullable FK to billing.Plan — null until the org
      subscribes to a plan (managed via Subscription).  The plain `plan`
      CharField has been removed; use `organization.subscription.plan` or
      `organization.billing_plan` for plan-level logic.
    - `stripe_customer_id` is null until the org subscribes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True, db_index=True)
    # Phase 4 — billing_plan replaces the old plan CharField.
    # Nullable: orgs without an active subscription have billing_plan=None.
    billing_plan = models.ForeignKey(
        "billing.Plan",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="organizations",
        help_text="Active plan tier. Managed via the Subscription model; do not set directly.",
    )
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    @property
    def plan_slug(self) -> str:
        """
        Convenience: return the plan slug string for rate-limit lookups.

        Falls back to 'FREE' when no billing_plan is linked so that
        unsubscribed orgs are treated as the lowest tier.
        """
        if self.billing_plan_id and self.billing_plan:
            return self.billing_plan.slug.upper()
        return "FREE"

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


# ── OrganizationInvitation ─────────────────────────────────────────────────────


class InvitationStatus(models.TextChoices):
    """
    Lifecycle states for an invitation token.

    Transitions:  PENDING → ACCEPTED (on successful accept)
                  PENDING → EXPIRED  (on manual revoke or TTL-based task)
    """

    PENDING = "PENDING", "Pending"
    ACCEPTED = "ACCEPTED", "Accepted"
    EXPIRED = "EXPIRED", "Expired"


class OrganizationInvitation(TimeStampedModel):
    """
    A secure, single-use invitation sent to an email address.

    Design decisions
    ────────────────
    - `token` is generated with `secrets.token_urlsafe(32)` (256 bits of
      randomness) — safe against brute-force enumeration.
    - Tokens are unique across the entire table (not just per-org), so URLs
      cannot be guessed from org ID + sequential IDs.
    - OWNER role is forbidden at invitation time (the OWNER is always the
      founder; ownership transfer is a separate, privileged operation).
    - A unique constraint on (organization, email, status=PENDING) is enforced
      at the application layer (see ``CreateInvitationSerializer``) — a DB
      partial unique index is added in the migration for safety.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
        db_index=True,
    )
    email = models.EmailField(db_index=True)
    role = models.CharField(
        max_length=20,
        choices=RoleEnum.choices,
        default=RoleEnum.MEMBER,
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        editable=False,
    )
    invited_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_org_invitations",
    )
    status = models.CharField(
        max_length=20,
        choices=InvitationStatus.choices,
        default=InvitationStatus.PENDING,
        db_index=True,
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Organization Invitation"
        verbose_name_plural = "Organization Invitations"
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["organization", "email"]),
        ]

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Invite {self.email} → {self.organization} ({self.status})"

    def accept(self, accepting_user) -> "OrganizationMembership":
        """
        Consume this invitation: create the membership and mark ACCEPTED.

        Assumes the caller has already verified that `accepting_user.email`
        matches ``self.email`` and that the invitation is still PENDING.
        """
        membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=accepting_user,
            role=self.role,
            invited_by=self.invited_by,
        )
        self.status = InvitationStatus.ACCEPTED
        self.save(update_fields=["status"])
        return membership
