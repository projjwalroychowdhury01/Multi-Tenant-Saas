# Codebase Review (April 17, 2026)

This review focuses on reliability, testability, and maintainability issues found during a quick pass through the backend codebase.

## Fixed in this branch

1. **Test bootstrap required `SECRET_KEY` from the environment**
   - **Issue:** `pytest` could fail before test collection if `SECRET_KEY` was missing from shell env.
   - **Impact:** Friction for local and CI runs; tests were not fully self-contained.
   - **Change:** Added a deterministic fallback `SECRET_KEY` in `config/settings/testing.py` before importing development settings.

2. **Redundant query pattern in `/auth/me` serialization**
   - **Issue:** `UserSerializer` computed membership independently for `org_id`, `org_slug`, and `role`, causing repeated DB lookups per request.
   - **Impact:** Extra SQL overhead on a hot endpoint.
   - **Change:** Added per-user membership memoization within serializer context.

3. **Duplicate test suites inside package `__init__.py`**
   - **Issue:** `tests/audit_logs/__init__.py` and `tests/usage/__init__.py` contained full duplicate test modules.
   - **Impact:** Noise, maintenance overhead, and potential accidental test drift.
   - **Change:** Replaced both files with minimal package markers.

4. **Unused token creation in registration view**
   - **Issue:** `register()` created a `RefreshToken.for_user(user)` object that was never used.
   - **Impact:** Minor overhead and confusing intent.
   - **Change:** Removed dead code.

## Additional improvement opportunities

1. **Fail-fast security checks in non-test envs**
   - Ensure `API_KEY_SECRET` and `SECRET_KEY` do not use development defaults in production deployments.
   - Consider adding startup validation that raises `ImproperlyConfigured` for weak/default secrets.

2. **Narrow exception handling in auth/tenant middleware**
   - Replace broad `except Exception` blocks with targeted exception classes and structured logging.
   - This improves observability for auth edge-cases while keeping middleware resilient.

3. **Document local developer bootstrap**
   - Expand `README.md` with minimal setup instructions (`.env`, DB, Redis, test command).
   - Current README is too sparse for new contributors.

4. **Adopt linting/format/type checks in CI**
   - Add a standard CI command set (`black --check`, `isort --check-only`, `flake8`, `mypy`, `pytest`) to avoid regressions.
