# Technical Requirements Document (TRD)
## Multi-Tenant SaaS Backend — Stripe-Style Architecture

**Version:** 1.0  
**Date:** 2026-04-11  
**Status:** Approved for Development  
**Audience:** Senior Engineers, DevOps, QA

---

## 1. Overview

This TRD translates the product requirements from the PRD into precise technical specifications. It covers system architecture, data models, middleware pipeline, service interfaces, infrastructure topology, and testing strategy. Every design decision prioritizes **correctness over cleverness**, **isolation over convenience**, and **explicit over implicit** behavior.

---

## 2. System Architecture

### 2.1 Layered Architecture

The system is composed of four distinct layers, each with a clearly defined responsibility boundary:

```
CLIENTS → API MIDDLEWARE PIPELINE → SERVICE LAYER → DATA LAYER
```

**Clients Layer** — Three types of consumers interact with the system:
- **Web/Mobile Applications** — authenticate using JWT Bearer tokens embedded in the `Authorization` header
- **Third-Party Integrations** — authenticate using Stripe-style API keys (`sk_live_...` / `sk_test_...`)
- **Admin/Internal Tooling** — uses session-based auth with optional 2FA, routed through Django Admin

**API Middleware Layer** — Every inbound request passes through a deterministic sequence of middleware components before reaching any view logic. These are described fully in Section 5.

**Service Layer** — Business logic is isolated into service modules (OrgService, BillingService, ApiKeyService, etc.). Views are thin orchestrators; services own the rules.

**Data Layer** — PostgreSQL as the primary OLTP store, Redis for ephemeral state (rate limit counters, token caches, usage counters, feature flag cache), and Celery workers for all async processing.

---

### 2.2 Multi-Tenancy Strategy: Shared Schema, Row-Level Isolation

**Chosen approach:** Single PostgreSQL database and schema shared across all tenants. Every tenant-scoped table has an `organization_id` foreign key column, and all ORM queries are automatically filtered to the current tenant's organization via a custom manager.

**Why shared schema was chosen over schema-per-tenant or database-per-tenant:**
- Simpler to operate, migrate, and back up at this scale
- No per-connection schema switching complexity
- Django ORM works natively
- Isolation correctness is enforced at the application layer, where it can be unit-tested deterministically

**Isolation correctness rule:** It must be architecturally impossible for a request authenticated as Tenant A to retrieve Tenant B's data through any ORM query. This is enforced by making the `TenantManager` the default manager on every tenant-scoped model, combined with an automated cross-tenant isolation test suite that is a hard CI gate (see Section 16).

---

## 3. Django Project Structure

The project follows a domain-driven app layout. Each Django app owns a distinct bounded context:

- **`apps/tenants/`** — Organization model, OrganizationMembership, TenantManager, TenantModel abstract base, SoftDeleteMixin, TimeStampedModel
- **`apps/users/`** — Custom User model (email-based, no username), JWT auth views, token blocklist
- **`apps/rbac/`** — Permission registry, HasTenantPermission DRF class, `@require_permission` decorator
- **`apps/api_keys/`** — ApiKey model, key generation/rotation/revocation, custom DRF authentication backend
- **`apps/billing/`** — Plan, Subscription, Invoice, UsageRecord models, BillingService interface, webhook handler
- **`apps/audit/`** — AuditLog model, AuditLogMiddleware, Celery write task, CSV export view
- **`apps/usage/`** — UsageMeterMiddleware, Redis counter logic, Celery flush task, usage summary view
- **`apps/features/`** — FeatureFlag model, evaluation logic, Redis cache layer, admin toggle
- **`apps/core/`** — Shared mixins, base views, health check endpoint, RequestId middleware

The `config/` directory holds settings split into `base`, `development`, and `production` files, plus `urls.py`, `celery.py`, and `wsgi.py`. All secrets are read from environment variables — never hard-coded.

The `tests/` directory mirrors the app structure with subdirectories for unit tests, integration tests, and the isolation test suite.

---

## 4. Core Data Models

All models use UUID primary keys and timezone-aware timestamps (`TIMESTAMPTZ`). All tenant-scoped models inherit from `TenantModel`, which enforces the `organization` foreign key and attaches the `TenantManager` as the default queryset manager.

---

### 4.1 Organization (Tenant Root)

The `Organization` model is the top-level unit of tenancy. Every other tenant-scoped record traces back to it via foreign key.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | Auto-generated on creation |
| `name` | VARCHAR(255) | Display name |
| `slug` | VARCHAR, unique, indexed | URL-safe identifier; human-readable public handle |
| `plan` | FK → Plan | Current subscription plan |
| `stripe_customer_id` | VARCHAR, nullable | External Stripe customer reference (null until billing set up) |
| `is_active` | BOOLEAN | Soft-activatable; deactivated orgs are blocked at middleware |
| `created_at` | TIMESTAMPTZ | Immutable creation timestamp |

The `slug` serves as the stable, human-readable public identifier for the organization in API paths and webhooks. It is enforced unique at the database level.

---

### 4.2 User (Custom AbstractUser)

The `User` model extends Django's `AbstractBaseUser`. The email address is the sole authentication identifier — there is no `username` field. This eliminates the classic username collision problem in multi-tenant systems where the same person may belong to multiple organizations.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | Auto-generated |
| `email` | VARCHAR, unique, indexed | Authentication identifier |
| `password_hash` | VARCHAR | Argon2 hashed — never stored or returned in plaintext |
| `full_name` | VARCHAR(255) | Display name |
| `is_verified` | BOOLEAN | Email verification flag |
| `last_login` | TIMESTAMPTZ | Set on each successful authentication |

A `User` record by itself has no tenant context. Tenant membership is governed by the `OrganizationMembership` join table.

---

### 4.3 OrganizationMembership (RBAC Join Table)

This model is the pivot between users and organizations. It stores the role a user holds within a specific organization. A single user can belong to multiple organizations with different roles in each.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `organization_id` | FK + composite index | References Organization |
| `user_id` | FK + composite index | References User |
| `role` | ENUM | One of: OWNER, ADMIN, MEMBER, VIEWER, BILLING |
| `invited_by` | FK User, nullable | Records who issued the invitation |
| `joined_at` | TIMESTAMPTZ | When the user accepted the invitation |

A composite unique constraint on `(organization_id, user_id)` ensures a user can only have one membership record (and therefore one role) per organization at a time. Role changes are done by updating the `role` field on the existing record.

---

### 4.4 TenantManager and TenantModel (Base Infrastructure)

`TenantManager` is a custom Django ORM manager that overrides `get_queryset()` to always inject a `filter(organization=get_current_org())` clause. This means that any model inheriting `TenantModel` is automatically scoped to the current tenant — no developer needs to remember to add the filter manually.

`get_current_org()` reads from a thread-local variable that is populated at the start of each request by the `TenantContextMiddleware` and cleared in a `finally` block at the end of every request — even if an exception occurs.

`TenantModel` also exposes `all_objects = models.Manager()` as an explicit escape hatch for use in migrations, Django Admin, and cross-tenant Celery tasks where the default scoping should not apply.

All tenant-scoped models inherit from `TenantModel` and thereby gain:
- Automatic tenant filtering on all ORM queries via `TenantManager`
- A `SoftDeleteMixin` (no hard deletes — see Section 13)
- A `TimeStampedModel` mixin (`created_at`, `updated_at` auto-fields)

---

### 4.5 ApiKey

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `organization_id` | FK + index | Tenant scope |
| `name` | VARCHAR | Human label for the key (e.g. "Production webhook key") |
| `prefix` | VARCHAR(12), indexed | First 12 characters of the key, stored in plaintext for fast DB lookup |
| `hashed_key` | VARCHAR(64) | SHA-256 HMAC of the full key — the only form ever persisted |
| `scopes` | ARRAY[VARCHAR] | Permission subset this key is authorized for |
| `env` | ENUM | `live` or `test` — enforces separation between production and test traffic |
| `created_by` | FK User, nullable | Attribution for audit purposes |
| `expires_at` | TIMESTAMPTZ, nullable | Optional expiration; null means non-expiring |
| `last_used_at` | TIMESTAMPTZ, nullable | Updated asynchronously on each use |
| `is_active` | BOOLEAN | Hard revocation flag; false means key is rejected immediately |

The full plaintext key is generated in memory, shown to the user exactly once via the API response, and then discarded. It is never written to disk, logs, or the database in any form.

---

### 4.6 AuditLog (Immutable Append-Only)

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `organization_id` | FK + index | Tenant scope |
| `actor_id` | FK User, indexed | Who performed the action (null for system-generated events) |
| `action` | VARCHAR | Namespaced event string, e.g. `user.role_changed`, `api_key.revoked` |
| `resource_type` | VARCHAR | Model name of the affected resource |
| `resource_id` | UUID, nullable | Primary key of the affected resource |
| `diff` | JSONB | Before-state and after-state of the mutation |
| `ip_address` | INET | Client IP address |
| `user_agent` | TEXT | Client User-Agent string |
| `request_id` | VARCHAR, indexed | Correlates this log entry with the originating HTTP request |
| `created_at` | TIMESTAMPTZ, indexed | Append-only creation timestamp |

No `UPDATE` or `DELETE` operation should ever be issued against this table by the application. In production, a database-level trigger or a restricted database role (with no `UPDATE`/`DELETE` privileges on this table) should enforce immutability at the infrastructure level. Records are partitioned by month using PostgreSQL table partitioning for tenants with high audit volume.

---

### 4.7 Billing Models

**Plan** — defines a tier of service. Its `limits` field is a JSONB object specifying numeric caps (e.g., `{"api_calls_per_month": 10000, "members_count": 5, "storage_mb": 500}`). Its `features` field is a JSONB boolean map of premium features (e.g., `{"audit_export": false, "advanced_rbac": true}`). Plans are seeded by developers and are not tenant-editable.

**Subscription** — the active link between an organization and a plan. Tracks billing status (`active`, `past_due`, `canceled`), the current period end date, and an optional cancellation timestamp. One organization can only have one active subscription at a time (enforced by a unique constraint on `organization_id`).

**Invoice** — a billing record generated each period. Stores the amount in integer cents (to avoid floating-point precision errors), status, and the payment timestamp. Linked to the `Subscription`.

**UsageRecord** — time-series records of API call counts per organization per hour. Written by the Celery Beat flush task that drains Redis counters into the DB. Used to compute current-period usage for plan enforcement and the usage summary endpoint.

---

### 4.8 FeatureFlag

| Field | Type | Notes |
|---|---|---|
| `key` | VARCHAR, unique | Stable identifier, e.g., `"advanced_rbac"` |
| `enabled_for_plans` | ARRAY[VARCHAR] | Plans that get this feature by default |
| `enabled_for_orgs` | ARRAY[UUID] | Org-level overrides (explicit enable/disable per org) |
| `rollout_pct` | INTEGER (0–100) | Percentage rollout for gradual feature release |
| `description` | TEXT | Internal documentation for the flag's purpose |
| `updated_at` | TIMESTAMPTZ | Tracks when the flag was last changed |

---

### 4.9 ResourceSnapshot (Data Versioning)

Every time a `VersionedMixin` model is saved, a `ResourceSnapshot` record is created with the full serialized JSON state of the resource at that point in time. This provides a complete, queryable history of how any record changed over time.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `organization_id` | FK + index | Tenant scope |
| `resource_type` | VARCHAR | Model class name |
| `resource_id` | UUID, indexed | PK of the versioned resource |
| `version` | INTEGER | Monotonically incrementing version number per resource |
| `snapshot` | JSONB | Full serialized state of the resource at this version |
| `created_by` | FK User, nullable | Who triggered the change |
| `created_at` | TIMESTAMPTZ, indexed | When this version was snapshotted |

A composite unique constraint on `(resource_type, resource_id, version)` prevents duplicate versions. The default ordering is by `version` descending, so the latest version is always returned first.

---

## 5. Middleware Pipeline

All middleware is registered in a strict, ordered sequence. The order is intentional and must not be changed without reviewing the downstream effects.

| Order | Middleware | Responsibility |
|---|---|---|
| 1 | `SecurityMiddleware` | HTTPS redirection, security headers |
| 2 | `CommonMiddleware` | URL normalization |
| 3 | `RequestIdMiddleware` | Injects a unique `X-Request-ID` into every request for log correlation |
| 4 | `JWTAuthMiddleware` / `ApiKeyAuthMiddleware` | Authenticates the request; resolves `request.user` and `request.org` |
| 5 | `TenantContextMiddleware` | Writes `request.org` into thread-local storage so `get_current_org()` works globally |
| 6 | `RBACMiddleware` | Attaches the authenticated user's permission set to the request object |
| 7 | `RateLimitMiddleware` | Enforces per-tenant, per-plan rate limits using Redis; returns `429` if over limit |
| 8 | `UsageMeterMiddleware` | Increments a Redis counter for the current org's API call usage (non-blocking) |
| 9 | `AuditLogMiddleware` | Captures request/response data for mutations; dispatches an async Celery write task |

---

### 5.1 Authentication Middleware (JWT Path)

For JWT-authenticated requests, the middleware reads the `Authorization: Bearer <token>` header, verifies the signature and expiry, and extracts the `user_id`, `org_id`, and `role` from the token payload. It then loads the `User` and `Organization` objects from the database (using a short-lived Redis cache where possible) and attaches them to `request.user` and `request.org`. If the token is expired, malformed, or revoked (via the blocklist), the request is rejected with `HTTP 401`.

---

### 5.2 Authentication Middleware (API Key Path)

For API-key-authenticated requests (`sk_live_...` or `sk_test_...`), the middleware extracts the key prefix (first 12 characters). It first checks a Redis cache keyed on the prefix for a cached `{org_id, scopes}` mapping (60-second TTL). On a cache miss, it queries the database for the `ApiKey` record matching the prefix, then performs a constant-time HMAC comparison to verify the submitted key against the stored hash. If verification succeeds, the resolved org and key scopes are cached in Redis. A Celery task is dispatched asynchronously to update the key's `last_used_at` timestamp — this never blocks the request.

---

### 5.3 TenantContextMiddleware

This middleware's only job is to call `set_current_org(request.org)` at the start of every request, making the current tenant available globally via `get_current_org()` without needing to thread the organization object through function arguments. Critically, the context is **always cleared in a `finally` block at the end of the request**, regardless of whether an exception occurred. This prevents thread reuse from carrying stale tenant context across requests in a threaded server environment.

---

### 5.4 RateLimitMiddleware

The rate limiter looks up the current organization's plan limit (e.g., `1,000 requests/minute` for PRO), then executes an atomic Redis Lua script implementing a sliding-window algorithm. The Lua script atomically prunes old entries from the window, counts the current entries, and conditionally adds the new entry — all in a single Redis round trip, preventing race conditions. If the limit is not exceeded, the request proceeds. If the limit is exceeded, the middleware immediately returns an `HTTP 429 Too Many Requests` response with a `Retry-After` header. Every response (allowed or denied) includes the standard `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers.

---

### 5.5 AuditLogMiddleware

For mutating requests (`POST`, `PATCH`, `PUT`, `DELETE`), the middleware captures the relevant request metadata — actor, organization, target resource, client IP, User-Agent, and request ID — before the view runs. After the view returns a successful response (status code below 400), it dispatches a Celery task to write the audit log entry to the database. The dispatch happens after the response is sent to avoid adding any latency. Sensitive fields (`password`, `hashed_key`, `secret_key`) are stripped from the diff before the Celery task is dispatched.

---

## 6. Authentication & JWT Design

### 6.1 Token Payload (Claims)

Every JWT access token issued by the system contains the following claims embedded in its payload:
- `user_id` — UUID of the authenticated user
- `org_id` — UUID of the organization context for this session
- `role` — The user's role within that organization (e.g., `"ADMIN"`)
- `exp` — Token expiry as a Unix timestamp
- `jti` — Unique JWT ID, used for revocation tracking

By embedding `org_id` and `role` in the token, the auth middleware can resolve tenant context and permissions without making any database queries on every request — the token is self-contained.

**Access token TTL:** 15 minutes  
**Refresh token TTL:** 7 days  
**Signing algorithm:** HS256 (development); RS256 with asymmetric key pair recommended for production  

### 6.2 Token Revocation (Blocklist)

When a user calls `POST /auth/logout`, the refresh token's `jti` claim is written into a Redis set called `revoked_tokens` with a TTL equal to the remaining token lifetime. At token refresh time, the system checks whether the presented refresh token's `jti` exists in the blocklist. If it does, the refresh is rejected and the user must re-authenticate.

### 6.3 Invitation Flow

Organization admins initiate an invitation via `POST /auth/invite`. The system generates a time-limited, signed invitation token using HMAC-SHA256. The payload encodes the `org_id`, `invitee_email`, pre-assigned `role`, and expiry timestamp. The signed token is embedded in an invitation link that is sent to the invitee via email (dispatched as a Celery task).

When the invitee clicks the link and calls `POST /auth/accept-invite/{token}`, the system:
1. Verifies the HMAC signature
2. Checks that the token has not expired (48-hour window)
3. Checks that the token has not already been used (single-use enforcement via Redis)
4. Creates or logs in the user, then creates the `OrganizationMembership` record with the pre-assigned role
5. Deletes the token from Redis to prevent re-use

---

## 7. RBAC Permission System

### 7.1 Permission Registry

The permission system is driven by a static, hard-coded dictionary (the "permission registry") that maps each role to its authorized set of permission strings. Permission strings follow a `resource:action` namespace convention — for example: `users:read`, `users:write`, `api_keys:manage`, `billing:read`, `billing:subscribe`, `audit_logs:export`.

This static registry means:
- Permission logic is reviewable as a single file — no hidden DB flags
- Adding a new permission is a single-source-of-truth change
- The CI role × endpoint matrix test verifies exhaustive coverage

**Role permission summary:**

| Role | Key Permissions |
|---|---|
| **OWNER** | Everything, including `org:delete`, `billing:subscribe`, `features:admin` |
| **ADMIN** | User management, role management, API key management, audit log access, billing read |
| **MEMBER** | Users read, API key read, usage read |
| **VIEWER** | Users read, API key read, usage read (identical to MEMBER; differentiated for semantic clarity) |
| **BILLING** | Billing read, users read only |

### 7.2 HasTenantPermission (DRF Permission Class)

Every DRF ViewSet declares a `required_permission` attribute. The `HasTenantPermission` class reads this attribute, looks up the authenticated user's role from the JWT claims on `request.auth_claims`, and checks whether that role possesses the required permission in the registry. If not, the request is rejected with `HTTP 403 Forbidden`.

### 7.3 `@require_permission` Decorator

For function-based views or service-layer guards, the `@require_permission("permission:string")` decorator provides the same check in a decorator pattern. It reads the current user's role from thread-local state and raises a `PermissionDenied` exception if the role is insufficient.

### 7.4 Object-Level Permissions

In addition to role-level checks, certain resources enforce ownership rules. For example, a `MEMBER` can edit their own resources but cannot modify resources owned by other users in the same org. Object-level permission checks are performed within the view or service layer after the resource is fetched, using the `has_object_permission` method pattern from DRF.

---

## 8. API Key System

### 8.1 Key Generation

When a new API key is created, the system:

1. Determines the environment prefix: `sk_live_` for production keys, `sk_test_` for test keys
2. Generates 256 bits of cryptographically random data using the OS-level CSPRNG (`secrets.token_urlsafe`)
3. Concatenates the prefix with the random data to form the full key (e.g., `sk_live_<random>`)
4. Extracts the first 12 characters as the `prefix` field for fast DB lookup
5. Computes an HMAC-SHA256 of the full key using a server-side secret (`API_KEY_SECRET` from environment). Only this hash is stored in the database.
6. Returns the full plaintext key to the caller in the API response — this is the only moment the full key is ever surfaced

The design guarantees that even a full database breach cannot recover valid API keys, because the values stored are HMAC hashes that cannot be reversed without also knowing `API_KEY_SECRET`.

### 8.2 Key Verification

When a request arrives with an API key bearer token:
1. The prefix (first 12 characters) is extracted from the submitted key
2. The database is queried for the `ApiKey` record with that prefix (one indexed lookup)
3. The HMAC of the submitted key is computed using `API_KEY_SECRET`
4. The computed HMAC is compared to `hashed_key` using a constant-time comparison function (to prevent timing attacks)
5. If they match, the key is valid and the associated organization is resolved

### 8.3 Key Rotation

Key rotation is designed for zero-downtime secret replacement:
1. `POST /api-keys/{id}/rotate` generates a new key and saves it as active
2. The old key's `expires_at` is set to 24 hours from now; it remains `is_active=True`
3. For 24 hours, both the old and new keys are valid simultaneously — allowing clients to cut over without a hard deadline
4. A Celery Beat task runs periodically and sets `is_active=False` on any key whose `expires_at` has passed

### 8.4 Redis Cache Layer

To avoid a DB lookup on every API request, the resolved `{org_id, scopes}` is cached in Redis keyed on the key prefix with a 60-second TTL. Cache misses trigger a full DB lookup + HMAC verification. This reduces database load dramatically for tenants making high API call volumes.

---

## 9. Rate Limiting

### 9.1 Algorithm: Sliding Window via Redis Lua

The rate limiter uses a sliding-window algorithm implemented as an atomic Lua script executed inside Redis. Lua scripts in Redis run atomically — no other commands can interleave during execution. This eliminates race conditions that would allow a burst to exceed the limit.

The algorithm works as follows:
- Each tenant's rate limit state is stored as a Redis Sorted Set
- Each entry in the set represents a single API call, scored by its timestamp
- On each new request, the Lua script first removes all entries older than the window size (making the window truly "sliding")
- It then counts the remaining entries. If the count is below the plan limit, it adds the new entry and the request is allowed
- If the count is at or above the limit, the request is denied without adding an entry

All of this happens in a single network round trip to Redis — no distributed locking is needed.

### 9.2 Plan-to-Limit Mapping

| Plan | Max Requests | Window |
|---|---|---|
| FREE | 100 | 60 seconds |
| PRO | 1,000 | 60 seconds |
| ENTERPRISE | 10,000 | 60 seconds |

The applicable limit is resolved per request by reading the current organization's subscription plan. For enterprise customers with custom contractual limits, the limit can be overridden at the `Organization` model level.

### 9.3 Rate Limit Headers

Every API response — both allowed and denied — includes these standard headers:

- **`X-RateLimit-Limit`** — the plan's maximum allowed requests per window
- **`X-RateLimit-Remaining`** — remaining requests in the current window
- **`X-RateLimit-Reset`** — Unix timestamp when the oldest entry exits the window and the count will decrease

When the limit is exceeded, the response is `HTTP 429 Too Many Requests` and includes a `Retry-After` header (in seconds) telling the client exactly how long to wait before retrying.

---

## 10. Billing System

### 10.1 BillingService Interface

The billing system is architected around a `BillingService` abstract interface with methods for: creating a customer, subscribing to a plan, canceling a subscription, and handling webhook events. This interface decouples the billing logic from any specific payment provider.

In development and CI, a `MockBillingService` is used that performs all operations against the local database without making any external network calls. It supports the same mock webhook events (`payment_succeeded`, `payment_failed`, `subscription_canceled`) so the full billing lifecycle can be tested without a real Stripe account.

In production, a `StripeBillingService` implementation of the same interface will make real Stripe API calls. Because all views call `BillingService` methods — never Stripe directly — the swap requires changing only the DI binding in `settings/production.py`. Zero view or service logic changes.

### 10.2 Webhook Handling

The `POST /billing/webhooks` endpoint receives incoming events from the billing provider. In mock mode, events are submitted directly. In production with real Stripe, the webhook signature header (`Stripe-Signature`) is verified against the webhook secret before any processing occurs — unsigned webhooks are rejected with `HTTP 400`.

The handler dispatches the appropriate action based on the event type:
- `payment_succeeded` → marks the invoice as paid, updates subscription status to `active`, dispatches a Celery task to send the invoice confirmation email
- `payment_failed` → updates subscription status to `past_due`, dispatches a notification email
- `subscription_canceled` → updates subscription status to `canceled`, sets the cancellation timestamp

### 10.3 Plan Enforcement

Before creating any resource that counts toward a plan limit, the service layer calls a `check_plan_limit()` helper. This function reads the current metric value (e.g., current member count) and compares it to the limit defined in the active plan's `limits` JSON field.

If the current value is at or above the hard limit and the 7-day grace period has passed, a `PlanLimitExceeded` exception is raised. The view layer catches this and returns an `HTTP 402 Payment Required` or `HTTP 403 Forbidden` response with a descriptive message pointing the user toward the billing upgrade flow.

The 7-day grace period is computed from `subscription.current_period_end`. This means a limit breach in the current period does not immediately block the customer — they have 7 days to upgrade before enforcement kicks in.

Feature gate checks work similarly: before returning premium features, the service checks whether the current plan's `features` JSON includes the required feature flag as `true`. If not, the feature is withheld regardless of the user's role.

---

## 11. Audit Logging

### 11.1 What is Captured

Every successful `POST`, `PATCH`, `PUT`, or `DELETE` request generates one audit log entry containing:

- **Actor** — the user (or API key's owner) who performed the action
- **Organization** — the tenant context
- **Action** — a namespaced string describing what happened (e.g., `user.role_changed`, `api_key.created`, `subscription.upgraded`)
- **Resource Type + Resource ID** — the affected model and its primary key
- **Diff** — a JSONB object with `"before"` and `"after"` sub-objects showing the state change. Fields classified as sensitive (`password_hash`, `hashed_key`, etc.) are stripped before storage
- **IP Address** — client IP (respects `X-Forwarded-For` behind a load balancer)
- **User Agent** — client application identifier
- **Request ID** — the `X-Request-ID` from the request, enabling cross-correlation with application logs

### 11.2 Write Strategy (Async, Never Blocking)

Capturing request state is synchronous — it happens in memory during request processing. But the actual database write is always dispatched as a Celery task after the response has been sent. This means audit logging adds zero observable latency to any API request. If the Celery broker is temporarily unavailable, tasks are queued and retried with exponential backoff.

### 11.3 Immutability Guarantee

The `AuditLog` model has no `update()` or `delete()` paths in application code. In production, a restricted database role that has only `INSERT` and `SELECT` privileges on the `audit_logs` table provides infrastructure-level enforcement. A database trigger can additionally reject any `UPDATE` or `DELETE` statements at the Postgres level.

### 11.4 Querying and Export

- `GET /audit-logs` returns a paginated list, filterable by `actor`, `action`, `resource_type`, and date range. Requires `ADMIN+` role.
- `GET /audit-logs/{id}` returns the full event detail including the before/after diff.
- `GET /audit-logs/export` returns a CSV file of the filtered log. Requires `ADMIN+` role. For large exports, this is streamed as a chunked response rather than buffered in memory.

---

## 12. Usage Metering

### 12.1 Real-Time Counting (Redis)

The `UsageMeterMiddleware` runs on every authenticated request. It increments a Redis counter using a single atomic `INCR` command. The Redis key is structured as `usage:{org_id}:{metric}:{YYYY-MM-DD-HH}`, separating counts by organization, metric type, and hour bucket. This is a sub-millisecond operation that adds negligible overhead to the request path.

### 12.2 Persistence (Celery Beat Flush)

A Celery Beat task runs every hour. It scans Redis for all `usage:*` keys, reads and atomically deletes each counter (`GETDEL`), and writes an aggregated `UsageRecord` row to PostgreSQL for each one. The `GETDEL` atomicity guarantees that no counts are lost between the read and delete, even if multiple Celery workers are running.

### 12.3 Usage Summary

`GET /usage/summary` computes the current period's total usage by summing `UsageRecord` quantities for the current billing period and comparing the total against the plan's `api_calls_per_month` limit. The response includes the current count, the plan limit, the percentage consumed, and the period reset date.

### 12.4 Threshold Alerts

After each hourly flush, a follow-up Celery task checks each organization's current-period usage:
- At **80% of the plan limit**: a `usage.warning` webhook event is fired and/or an email is sent to the OWNER. This alert is idempotent — a Redis key tracks whether the 80% alert has already been sent for the current period, preventing repeated spam.
- At **100% of the plan limit**: a `usage.exceeded` event is fired. The grace period clock begins.

---

## 13. Feature Flags

### 13.1 Evaluation Logic

When `is_feature_enabled(org, key)` is called, the system evaluates three conditions in sequence:

1. **Plan-level default** — Is the organization's current plan listed in `enabled_for_plans` for this flag? If yes → enabled.
2. **Org-level override** — Is this specific organization's UUID listed in `enabled_for_orgs`? If yes → enabled (regardless of plan). This allows enabling/disabling a flag for specific tenants outside their plan's defaults.
3. **Rollout percentage** — Is the organization within the rollout bucket? The bucket is determined by taking the integer hash of the org's UUID modulo 100 and checking if it falls below `rollout_pct`. This is deterministic per org — the org is always in or out of the same rollout — with no random drift across evaluations.

If none of the three conditions are true, the feature is disabled for that org.

### 13.2 Redis Cache Layer

The result of every flag evaluation is cached in Redis under a key like `feature:{org_id}:{flag_key}` with a 60-second TTL. This means at most one Redis lookup per 60 seconds per org per flag, with no database queries at all during that window. When a flag is changed in the Django Admin, the admin action explicitly deletes the affected Redis cache keys to provide near-instant propagation.

### 13.3 Admin Control

The Django Admin provides a UI for SUPERUSER accounts to flip any feature flag for any tenant in real time. Flag changes propagate to all application instances within 60 seconds (maximum TTL) without requiring a deploy or restart.

---

## 14. Soft Deletes & Data Versioning

### 14.1 Soft Deletes

All tenant-scoped models inherit `SoftDeleteMixin`. When a resource is "deleted" via the API, no `DELETE` SQL statement is executed. Instead, the model's `soft_delete()` method sets `deleted_at = now()` and `deleted_by = current_user`, then saves the record.

The `SoftDeleteMixin` replaces the default queryset manager with one that filters out soft-deleted records by default (`.filter(deleted_at__isnull=True)`). This means all normal ORM queries continue to work transparently — they naturally exclude deleted records. Two explicit escape hatches are provided:
- `.alive()` — equivalent to the default; explicitly excludes deleted records
- `.deleted()` — returns only soft-deleted records (for admin/recovery views)

`POST /resources/{id}/restore` allows an `ADMIN+` to restore a soft-deleted resource by setting `deleted_at = null` and `deleted_by = null`.

### 14.2 Data Versioning

`VersionedMixin` instruments the `save()` method. On every save (after the initial create), it increments the `version` integer field using a database-level atomic increment to prevent version collision under concurrent writes.

After each successful save, a Celery task is dispatched to create a `ResourceSnapshot` entry containing the full serialized JSON state of the resource at that version. The Celery dispatch is asynchronous and does not block the save response.

`GET /resources/{id}/history` returns the full version timeline — all `ResourceSnapshot` records for that resource ordered by version descending — allowing users and administrators to inspect exactly what changed and when.

---

## 15. Technology Stack

| Component | Technology | Justification |
|---|---|---|
| **Framework** | Django 5.x + DRF 3.x | Battle-tested, extensive ecosystem, powerful admin interface |
| **Primary Database** | PostgreSQL 16 | JSONB fields, INET type, table partitioning, Row-Level Security support |
| **Cache / Rate Limiting** | Redis 7 | Atomic Lua scripts, pub/sub, sub-millisecond INCR |
| **Task Queue** | Celery 5 + Celery Beat | Async writes, scheduled jobs, retry with exponential backoff |
| **JWT** | djangorestframework-simplejwt | Mature library; supports token rotation, blocklist, customizable claims |
| **API Documentation** | drf-spectacular | Auto-generates OpenAPI 3.0 schema from DRF views and type hints |
| **Testing** | pytest + pytest-django + factory_boy | Database fixtures, parametrize, model factories |
| **Load Testing** | Locust | Simulates concurrent API traffic for rate limit and latency validation |
| **Dependency Management** | Poetry or uv | Reproducible lock files; deterministic installs in CI |
| **Containerization** | Docker + Docker Compose | Isolated services: `web`, `db`, `redis`, `celery`, `celery-beat` |
| **CI/CD** | GitHub Actions | Linting → tests → coverage gate → Docker build |
| **Structured Logging** | structlog | JSON-formatted log output with request context binding |
| **Error Tracking** | Sentry | Exception capture with org/user/request context as Sentry tags |
| **Code Quality** | black, isort, flake8, mypy | Enforced via pre-commit hooks on every commit |
| **Password Hashing** | argon2 (via `django[argon2]`) | Memory-hard algorithm; preferred over bcrypt/PBKDF2 for modern systems |

---

## 16. Infrastructure: Docker Compose Services

The local development environment is fully containerized via Docker Compose with five services:

- **`web`** — The Django application server. Runs `manage.py runserver` in development; in production, this is replaced with Gunicorn behind an Nginx reverse proxy.
- **`db`** — PostgreSQL 16. Data is persisted in a named Docker volume so it survives container restarts.
- **`redis`** — Redis 7. Serves as both the Celery broker and the application cache. Uses a separate Redis database index for each purpose (e.g., `/0` for Celery, `/1` for app cache).
- **`celery`** — Celery worker process consuming the task queue. Handles all async tasks: audit log writes, API key `last_used_at` updates, billing emails, usage threshold alerts.
- **`celery-beat`** — Celery Beat scheduler. Manages periodic tasks: hourly usage flush, daily billing aggregation, API key expiry cleanup. Uses the Django database scheduler so beat schedules survive restarts.

All inter-service credentials (DB URL, Redis URL, secret keys) are passed as environment variables from a `.env` file that is never committed to source control.

---

## 17. Testing Strategy

### 17.1 Test Layers

| Layer | Scope | CI Gate? |
|---|---|---|
| **Unit Tests** | Service functions, permission registry, rate limit algorithm logic, feature flag evaluation | Yes — coverage threshold |
| **Integration Tests** | Full HTTP request → DRF view → DB → response cycle | Yes — all must pass |
| **Cross-Tenant Isolation Tests** | Org A cannot access, list, or modify Org B's resources | Yes — **100% pass rate required, zero tolerance** |
| **Permission Matrix Tests** | Every role × every endpoint returns the expected 200/403 | Yes — hard gate |
| **Load Tests** | Rate limit behavior under concurrent requests | Optional — run pre-release |

### 17.2 Cross-Tenant Isolation Test Pattern

The isolation test suite is the most critical test category. For every tenant-scoped resource type and every read/write endpoint, the test:
1. Creates Organization A and Organization B in the test database
2. Creates a resource belonging to Organization A
3. Authenticates as an `ADMIN` of Organization B (the strongest non-OWNER role granted to the "attacker")
4. Attempts to `GET`, `PATCH`, or `DELETE` the Organization A resource using Organization B's valid JWT
5. Asserts `HTTP 404` (not 403, because returning 403 would confirm that the resource exists — a data leak in itself)

This suite must pass at 100% — any failure indicates a tenant isolation regression and blocks the merge.

### 17.3 Permission Matrix Test Pattern

Using `pytest.mark.parametrize`, a single test function is parameterized across all combinations of `(role, endpoint, expected_status)`. Every endpoint is declared in a test configuration table alongside its minimum required role. The test creates a user with each of the five roles and verifies that the endpoint returns the expected HTTP status code. This matrix is the source of truth for what roles are allowed where, and it enforces that the permission registry matches the actual behavior.

### 17.4 Factory Boy Fixtures

`factory_boy` is used to define model factories for all major models (`OrganizationFactory`, `UserFactory`, `MembershipFactory`, `ApiKeyFactory`, `PlanFactory`, etc.). Tests compose these factories to build realistic data graphs without hardcoded fixtures. Related objects are created via `SubFactory` declarations, keeping test setup compact and readable.

### 17.5 GitHub Actions CI Pipeline

The CI pipeline runs on every pull request and blocks merge on failure. Steps in order:

1. **Pre-commit hooks** — `black` (formatting), `isort` (imports), `flake8` (linting), `mypy` (type checking)
2. **Unit + Integration tests** — full `pytest` run with `--cov` coverage reporting; fails if coverage drops below 85%
3. **Isolation suite** — cross-tenant isolation tests run as a separate step with 100% required pass rate
4. **Schema validation** — `drf-spectacular` schema generation with `--fail-on-warn` flag
5. **Docker build** — verifies the production Dockerfile builds cleanly

---

## 18. API Key Security Considerations

| Threat | Mitigation |
|---|---|
| Leaked key in application logs | All `Authorization` header values are redacted before any logging middleware processes them |
| Leaked key in URL parameters | Keys are only accepted in the `Authorization` header — never as a query parameter |
| Database breach | Only the HMAC hash is stored. Without `API_KEY_SECRET`, hashes cannot be reversed |
| HMAC timing attacks | Comparison always uses a constant-time function (`hmac.compare_digest`) to prevent timing side-channels |
| Brute force against prefix | The 256-bit random suffix makes enumeration computationally infeasible |
| Key enumeration via error messages | Failed key lookups return `HTTP 401` with a generic message — no indication of whether the prefix exists |
| Key replay after revocation | Setting `is_active=False` takes effect immediately. The Redis cache TTL is 60 seconds, so revoked keys may work for up to 60 seconds — acceptable for the use case; use explicit `CACHE_DEL` on revoke if stricter guarantees are needed |

---

## 19. Database Index Summary

| Table | Indexed Columns | Query Pattern |
|---|---|---|
| `Organization` | `slug` | URL lookup by slug |
| `OrganizationMembership` | `(organization_id, user_id)` | Per-request membership + role check |
| `ApiKey` | `prefix`, `organization_id` | Key lookup; org-scoped key listing |
| `AuditLog` | `(organization_id, created_at)`, `actor_id` | Time-range queries, actor filtering |
| `UsageRecord` | `(organization_id, metric, recorded_at)` | Period aggregation for plan enforcement |
| `FeatureFlag` | `key` | Flag lookup by key name |
| `Subscription` | `organization_id` | One-to-one fetch per request |
| `ResourceSnapshot` | `(resource_type, resource_id)`, `version` | Version history queries |

---

## 20. Health Check Endpoint

`GET /health` is publicly accessible (no authentication required) and returns a JSON status report for the system and its critical dependencies.

The response body includes:
- **`status`** — `"ok"` if all checks pass, `"degraded"` if any check fails
- **`checks`** — an object with individual status for `database`, `redis`, and `celery` (`"ok"` or `"unavailable"`)
- **`version`** — the application version string
- **`timestamp`** — current server-side UTC timestamp

The database check performs a trivial `SELECT 1` query. The Redis check performs a `PING` command. The Celery check verifies that at least one worker is responsive by inspecting the active worker list.

HTTP status code is `200` when healthy and `503 Service Unavailable` when degraded. This contract allows load balancers and uptime monitors to interpret the response correctly.

---

## 21. Observability

### 21.1 Structured Logging (structlog)

All application logs are emitted as single-line JSON objects. Every log line at minimum includes: `level`, `event`, `timestamp`, `request_id`, `org_id`, and `user_id` (where available). The `request_id` field is the `X-Request-ID` injected by `RequestIdMiddleware`, enabling full trace reconstruction across all log lines produced by a single request — even across async tasks that inherit the request ID.

### 21.2 Sentry Integration

Sentry is configured in `settings/production.py` via a DSN from an environment variable. All unhandled exceptions are captured with their full stack trace and the current request context. The middleware layer enriches Sentry's scope with `org_id`, `user_id`, and `request_id` as tags, enabling fast filtering of errors by tenant in the Sentry UI. Performance tracing is enabled at a configurable sample rate (default: 10%).

### 21.3 OpenAPI 3.0 Schema (drf-spectacular)

The OpenAPI schema is auto-generated from DRF view annotations, serializer definitions, and type hints. It is served at `/api/schema/` (YAML format) and `/api/docs/` (Swagger UI). Every endpoint is documented with its request schema, response schema, authentication requirement, and possible error codes. Schema generation is validated in CI with `--fail-on-warn` to catch missing annotations.

---

## 22. Environment Configuration

All secrets and infrastructure addresses are provided via environment variables. The `django-environ` library handles `.env` file parsing and type coercion. Required variables include:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django cryptographic key |
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection string (app cache) |
| `CELERY_BROKER_URL` | Redis connection string (Celery broker) |
| `API_KEY_SECRET` | 32-byte secret used for API key HMAC computation |
| `JWT_SIGNING_KEY` | Secret for signing JWT tokens |
| `SENTRY_DSN` | Sentry project DSN (empty in development = Sentry disabled) |
| `EMAIL_BACKEND` | Django email backend (console in dev, SMTP in production) |
| `DEBUG` | Must be `False` in production |
| `ALLOWED_HOSTS` | Comma-separated list of valid hostnames |

---

## 23. Security Checklist

| Item | Status |
|---|---|
| `DEBUG=False` enforced in production settings | Required |
| `ALLOWED_HOSTS` explicitly set | Required |
| HTTPS enforced (`SecurityMiddleware` HSTS headers) | Required |
| CSRF protection on session-based admin views | Required |
| API serializers expose only declared fields (no `fields = "__all__"` in production serializers) | Required |
| `password_hash` and `hashed_key` never appear in API response JSON | Required |
| Invitation tokens expire after 48 hours and are single-use | Required |
| All ORM queries use parameterized inputs (no raw SQL string interpolation) | Required |
| Audit log rows are append-only at the database role level | Required |
| `/auth/token` login endpoint is rate-limited (10 attempts/minute per IP) | Required |
| Password hashing uses argon2 (configured via `PASSWORD_HASHERS` in settings) | Required |
| `X-Content-Type-Options: nosniff` header set | Required |
| `X-Frame-Options: DENY` header set | Required |
| `Authorization` headers redacted in all log outputs | Required |
| `API_KEY_SECRET` and `JWT_SIGNING_KEY` sourced from environment, never committed to source control | Required |
