"""
Custom User model.

Key decisions:
  - No `username` field — email is the sole authentication identifier.
  - UUID primary key — avoids sequential ID enumeration attacks.
  - Integrates with Django's auth framework via AbstractBaseUser +
    PermissionsMixin, so Django Admin, groups, and permissions all work.
  - `is_verified` tracks email verification; unverified users can still
    log in but may be gated from certain features (Phase 2+).
"""

import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

from apps.core.mixins import TimeStampedModel


class UserManager(BaseUserManager):
    """Manager that creates users identified by email, not username."""

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_verified", True)

        if not extra_fields.get("is_staff"):
            raise ValueError("Superuser must have is_staff=True.")
        if not extra_fields.get("is_superuser"):
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """
    System-wide user account.

    A single User can be a member of multiple Organizations — each
    membership has its own role (see OrganizationMembership).
    The User model itself is not tenant-scoped.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    full_name = models.CharField(max_length=255, blank=True)
    is_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)  # required for Django Admin access

    objects = UserManager()

    # Override AbstractBaseUser field — last_login auto-updated by Django on token obtain
    last_login = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # email is already included via USERNAME_FIELD

    class Meta:
        ordering = ["email"]
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self):
        return self.email

    @property
    def display_name(self):
        return self.full_name or self.email
