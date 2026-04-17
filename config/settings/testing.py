"""
Test settings — uses SQLite and in-memory cache so tests run
without any external services (no Docker required).
"""

import os

# Ensure test settings can be imported without requiring a local .env file.
# Keep length >= 32 bytes to avoid JWT HMAC key-length warnings in tests.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-local-ci-only-32bytes")

from .development import *  # noqa: F401, F403

# Force SQLite for tests
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Use in-memory cache for tests (no Redis required)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Speed up password hashing in tests
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Use console email backend
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Celery always eager in tests — tasks run synchronously
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Disable Redis-backed rate limiting in tests — the LocMemCache backend
# does not expose the raw Redis client needed for Lua scripts.
RATE_LIMIT_ENABLED = False

# Deterministic HMAC secret for test key generation
API_KEY_SECRET = "test-api-key-hmac-secret-32bytes!"
