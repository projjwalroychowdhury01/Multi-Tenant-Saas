"""
ApiKey model — Stripe-style API key design.

Key format
──────────
  sk_live_<random>  — production environment key
  sk_test_<random>  — test environment key

Security pattern
────────────────
  1. On creation  : generate a cryptographically random secret.
  2. Derive prefix: first 12 chars of the secret (stored plain-text for lookup).
  3. Hash secret  : HMAC-SHA256(secret, settings.API_KEY_SECRET) stored in DB.
  4. Return once  : the full plaintext secret is returned ONCE in the creation
                   response. It is NEVER stored and NEVER returned again.
  5. Verification : incoming Bearer token → lookup by prefix → re-hash →
                   compare against stored hashed_key.

Rotation (24-hour overlap)
──────────────────────────
  POST /api-keys/{id}/rotate issues a brand-new key and sets the old key's
  expires_at = now + 24 hours.  The old key keeps working during the overlap
  window to allow clients to swap their secrets without downtime.
"""

import hashlib
import hmac
import secrets
import uuid

from django.conf import settings
from django.db import models

from apps.core.mixins import TimeStampedModel
from apps.tenants.models import TenantModel


class EnvChoices(models.TextChoices):
    LIVE = "live", "Live"
    TEST = "test", "Test"


class ApiKey(TenantModel, TimeStampedModel):
    """
    Tenant-scoped API key.

    Inherits TenantModel so every queryset is auto-scoped to the current org.
    The full secret is NEVER persisted — only the HMAC hash and prefix are.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="Human-readable label for this key.")
    prefix = models.CharField(
        max_length=16,
        unique=True,
        db_index=True,
        help_text="Plain-text prefix for fast DB lookup (e.g. 'sk_live_abc1').",
    )
    hashed_key = models.CharField(
        max_length=128,
        help_text="HMAC-SHA256 of the full secret. Never the secret itself.",
    )
    env = models.CharField(
        max_length=10,
        choices=EnvChoices.choices,
        default=EnvChoices.LIVE,
    )
    # Scopes are namespaced permission strings the key may use.
    # Empty list = inherits all permissions of the creating user's role.
    scopes = models.JSONField(
        default=list,
        blank=True,
        help_text="List of permission scope strings this key is restricted to.",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Optional hard expiry. Null = never expires.",
    )
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set asynchronously via Celery to avoid blocking requests.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_api_keys",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "API Key"
        verbose_name_plural = "API Keys"
        indexes = [
            models.Index(fields=["prefix", "is_active"]),
            models.Index(fields=["organization", "is_active"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    # ── Key generation helpers (class-level, no DB access) ────────────────────

    @staticmethod
    def generate_secret(env: str = EnvChoices.LIVE) -> str:
        """
        Generate a cryptographically random secret key string.

        Format: sk_{env}_{url-safe random 32 bytes as hex}
        Example: sk_live_4f3a...
        """
        random_part = secrets.token_hex(32)  # 64-char hex string
        return f"sk_{env}_{random_part}"

    @staticmethod
    def derive_prefix(secret: str) -> str:
        """
        Extract the lookup prefix from the full secret.

        We take the scheme + first 4 chars of the random part so the prefix
        is unique and human-scannable without revealing the secret.
        Example: 'sk_live_4f3a'
        """
        # secret = "sk_live_4f3a..."
        parts = secret.split("_")
        if len(parts) < 3:  # pragma: no cover
            raise ValueError("Malformed API key secret")
        random_section = parts[2]  # '4f3a...'
        return f"sk_{parts[1]}_{random_section[:4]}"

    @staticmethod
    def hash_secret(secret: str) -> str:
        """
        Return HMAC-SHA256(secret, API_KEY_SECRET) as a hex string.

        Uses a pepper (API_KEY_SECRET) separate from Django's SECRET_KEY so
        that a DB dump alone is not sufficient to brute-force the secrets.
        """
        pepper = settings.API_KEY_SECRET.encode()
        mac = hmac.new(pepper, secret.encode(), hashlib.sha256)
        return mac.hexdigest()

    @staticmethod
    def verify_secret(secret: str, hashed_key: str) -> bool:
        """Constant-time comparison to prevent timing attacks."""
        expected = ApiKey.hash_secret(secret)
        return hmac.compare_digest(expected, hashed_key)
