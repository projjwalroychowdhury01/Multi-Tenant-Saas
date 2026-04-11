# Product Requirements Document (PRD)
## Multi-Tenant SaaS Backend — Stripe-Style Architecture

**Version:** 1.0  
**Date:** 2026-04-11  
**Status:** Approved for Development  
**Audience:** Engineering, Product, QA

---

## 1. Overview

This PRD defines the requirements for building a **production-grade, multi-tenant SaaS backend** from scratch. The project is architected in the style of Stripe's backend — meaning strict tenant isolation, developer-facing API key management, plan-based rate limiting, an immutable audit trail, and pluggable billing hooks.

The goal is to produce a backend that:
- Demonstrates real-world, senior-level backend engineering depth  
- Can serve as a portfolio anchor project positioned toward "Production SaaS Backend Engineer" roles  
- Is extensible so that a real Stripe integration, a frontend dashboard, or additional product features can be bolted on without architectural changes

**Estimated Timeline:** 6–8 weeks across 6 build phases  
**Primary Stack:** Django + Django REST Framework, PostgreSQL, Redis, Celery

---

## 2. Problem Statement

Building a scalable SaaS product requires a backend that solves several non-trivial problems simultaneously:

| Problem | Consequence if Ignored |
|---|---|
| Multiple paying customers sharing one database | Data leakage across tenants = catastrophic |
| No role system | Any user can perform any action |
| Flat API — no key management | Cannot give programmatic access to third parties |
| No rate limiting | One misbehaving tenant can take down all others |
| No billing enforcement | Feature abuse, plan tier violations |
| No audit trail | Compliance failures, undetectable insider threats |

This project addresses all six problems in a principled, production-safe manner.

---

## 3. Goals & Non-Goals

### 3.1 Goals

- **G1 — Multi-Tenant Isolation:** Every database query is automatically scoped to the authenticated tenant's organization. Cross-tenant data leakage must be impossible at the ORM level.
- **G2 — Authentication:** Secure JWT-based login with organization context embedded in token claims. Support refresh token flow and token revocation/blocklist.
- **G3 — Invitation Flow:** Organization owners invite users via email link. Invited users join with a pre-assigned role.
- **G4 — RBAC:** Five-tier role hierarchy controlling access to every API endpoint and resource action.
- **G5 — API Key System:** Stripe-style `sk_live_` / `sk_test_` key pairs with scoped permissions, rotation, and revocation. Secret shown exactly once.
- **G6 — Rate Limiting:** Per-tenant, per-plan rate limits enforced via Redis sliding-window algorithm. Standard HTTP response headers.
- **G7 — Billing Hooks:** Plan/Subscription/Invoice models with a mock Stripe webhook handler that can be swapped for real Stripe with zero architectural changes.
- **G8 — Plan Enforcement:** Hard limits on member count, API calls per month, and storage. Grace period before enforcement.
- **G9 — Audit Logs:** Immutable, append-only event log capturing every mutation with actor, diff, IP, and user-agent. Async write so it never blocks requests.
- **G10 — Usage Metering:** Per-tenant, per-hour API call counters in Redis flushed to PostgreSQL by a scheduled Celery task.
- **G11 — Feature Flags:** Per-tenant feature toggles with plan-based defaults and real-time updates via Redis cache.
- **G12 — Data Versioning & Soft Deletes:** No hard deletes; full JSON snapshot on every write; restore and version history endpoints.
- **G13 — Observability:** Health check endpoint, structured JSON logging, Sentry integration, OpenAPI 3.0 schema.

### 3.2 Non-Goals (v1)

- A frontend UI or dashboard (the API is the product)
- Real Stripe payment processing (mock integration only, real Stripe can swap in later)
- Multi-region database sharding or schema-per-tenant isolation (shared schema is intentional)
- Mobile SDK wrappers
- Self-serve organization deletion

---

## 4. User Personas & Roles

### 4.1 Who Uses This System?

| Actor | Description |
|---|---|
| **Organization Owner** | Founder / admin who signs up, creates the organization, manages billing |
| **Admin** | Senior team member who manages users, roles, and API keys |
| **Member** | Standard team member who reads and writes own resources |
| **Viewer** | Read-only stakeholder (e.g., compliance, analyst) |
| **Billing Manager** | Finance team; can only see billing pages |
| **Third-party Integration** | External service authenticating with an API key |

### 4.2 Role Hierarchy

```
OWNER > ADMIN > MEMBER > VIEWER
                       > BILLING
```

| Role | Capabilities |
|---|---|
| **OWNER** | Full control including deleting the organization, managing billing, all admin actions |
| **ADMIN** | Manage users, roles, API keys, and settings. Cannot delete the org |
| **MEMBER** | Read + write own resources within the tenant |
| **VIEWER** | Read-only access to all tenant resources |
| **BILLING** | Read access + billing/invoice pages only |

---

## 5. Core Features

### 5.1 Multi-Tenant Isolation

**How it works:**  
A shared PostgreSQL schema is used (all tenants in the same database). Every tenant-owned table has an `organization_id` foreign key column. A custom Django ORM manager (`TenantManager`) automatically filters every queryset using the current request's tenant context stored in thread-local storage. No developer needs to remember to add `.filter(organization=...)` — the scoped manager does it globally.

**Key requirements:**
- `TenantModel` abstract base class that all tenant-scoped models inherit from
- `TenantManager.get_queryset()` always injects `filter(organization=get_current_org())`
- `all_objects = models.Manager()` escape hatch for admin/migration use only
- Soft-delete mixin on all tenant models (no hard deletes)
- Cross-tenant isolation verified by automated tests: org A cannot retrieve org B's resources

---

### 5.2 Authentication & Organization Registration

**Registration flow:**
1. `POST /auth/register` — creates both the `User` and an `Organization` atomically in a single DB transaction. The registering user is assigned the `OWNER` role in the new org's `OrganizationMembership` table.
2. The created JWT access token embeds `org_id`, `user_id`, and `role` directly in the claims — no extra DB lookup needed per request.
3. Refresh tokens are issued alongside access tokens. Expired access tokens can be refreshed without re-login.
4. Logout invalidates the refresh token by adding it to a Redis or DB blocklist.
5. `POST /auth/invite` — ADMIN+ generates an invitation link (signed HMAC token) and sends it via email (Celery task).
6. `POST /auth/accept-invite/{token}` — validates the invitation token, creates/logs in the invited user, creates their `OrganizationMembership` with the pre-assigned role.

**JWT customization:**
- Token payload includes: `user_id`, `org_id`, `role`, `exp`, `jti`
- Custom authentication middleware resolves tenant from token on every request
- Per-request tenant context is injected into Django's thread-local storage so `get_current_org()` works globally

---

### 5.3 Role-Based Access Control (RBAC)

**Permission registry:**  
A static mapping of `role → set[permission_string]` drives authorization. Permissions are string-namespaced: `users:read`, `users:write`, `api_keys:manage`, `billing:read`, `billing:write`, `audit_logs:read`, etc.

**DRF Integration:**
- Custom DRF permission class `HasTenantPermission` checks the permission registry on every view
- `@require_permission("users:write")` decorator for view-level enforcement
- Object-level permissions for resource ownership (a MEMBER cannot modify another MEMBER's data)
- `GET /me/permissions` returns the full permission list for the currently authenticated user

**Testing requirement:**  
A role × endpoint matrix test must verify that every combination of (role, endpoint) returns either `200` or `403` as expected. This test is a hard gate in CI.

---

### 5.4 API Key System

Inspired by Stripe's API key design:

**Key format:**
- `sk_live_<random>` — production environment key
- `sk_test_<random>` — test environment key
- Key prefix (e.g., `sk_live_abc1`) is stored in plain text for lookup
- The full key is SHA-256 hashed (HMAC) and stored. **The plaintext key is shown to the user exactly once** at creation time.

**Lifecycle:**
- `POST /api-keys` — create key (returns plaintext once only)
- `GET /api-keys` — list keys (shows name, prefix, last 4 chars, scopes, last_used_at — never full secret)
- `PATCH /api-keys/{id}` — update name, scopes, or expiry date
- `DELETE /api-keys/{id}` — immediately revokes the key (sets `is_active = false`)
- `POST /api-keys/{id}/rotate` — issues a new key; old key remains valid for a 24-hour overlap window then expires automatically

**Authentication via API key:**
- Custom DRF `Authentication` class parses `Authorization: Bearer sk_live_...` headers
- Lookup by prefix → verify HMAC of full key → resolve organization
- Redis cache stores `hashed_key → org_id` with 60-second TTL to reduce DB hits
- `last_used_at` is updated asynchronously via a Celery task (non-blocking)

---

### 5.5 Rate Limiting

**Algorithm:** Redis sliding-window implemented as an atomic Lua script

**Key structure:** `rate:{org_id}:{window_start}` — per-tenant, per-time-window counter

**Plan-based limits:**
| Plan | Limit |
|---|---|
| FREE | 100 requests/minute |
| PRO | 1,000 requests/minute |
| ENTERPRISE | 10,000 requests/minute |

**HTTP response headers on every request:**
- `X-RateLimit-Limit` — plan limit
- `X-RateLimit-Remaining` — requests remaining in current window
- `X-RateLimit-Reset` — Unix timestamp when the window resets

**Rate exceeded behavior:**
- Returns HTTP `429 Too Many Requests`
- Includes `Retry-After` header with seconds until window reset

---

### 5.6 Billing Hooks & Subscription Plans

**Data models:**
- `Plan` — name, price_monthly, limits (JSON field), features (JSON field)
- `Subscription` — org, plan, status (`active` / `past_due` / `canceled`), current_period_end, cancel_at
- `Invoice` — subscription, amount_cents, status, paid_at
- `UsageRecord` — org, metric_name, quantity, recorded_at

**API endpoints:**
- `GET /billing/plans` — list all available plans  
- `GET /billing/subscription` — current org subscription with status  
- `POST /billing/subscribe` — upgrade or downgrade plan  
- `GET /billing/invoices` — invoice history  
- `POST /billing/webhooks` — webhook handler for Stripe events (signature-verified even in mock mode)

**Mock Stripe integration:**
- `BillingService` interface in the service layer; the mock implementation can be replaced by a real `stripe-python` implementation with zero changes to the views/controllers.
- Supported mock webhook events: `payment_succeeded`, `payment_failed`, `subscription_canceled`
- Celery task: send invoice confirmation email on `payment_succeeded`
- Celery Beat: daily aggregation of `UsageRecord` entries

**Plan enforcement:**
- `check_plan_limit(org, limit_type)` helper called in the service layer before creating resources
- Enforced limits: `members_count`, `api_calls_per_month`, `storage_mb`
- 7-day grace period before hard enforcement kicks in after limit is exceeded
- Feature gate check: `plan includes feature_x?` evaluated before allowing access to premium features

---

### 5.7 Audit Logs

**Purpose:** Compliance, security forensics, and debugging. Every mutation (POST, PATCH, DELETE) must be logged before it is forgotten.

**What is captured per event:**
- `actor` — the user (or API key) that performed the action
- `organization` — the tenant context
- `action` — namespaced action string, e.g., `user.role_changed`, `api_key.revoked`
- `resource_type` + `resource_id` — the affected object
- `diff` — JSONB field with before-state and after-state
- `ip_address`, `user_agent`, `request_id`
- `created_at` — indexed for fast range queries; **records are never updated or deleted**

**Write strategy:** Async via Celery. The `AuditLogMiddleware` collects event data synchronously but dispatches the write task asynchronously — requests are never blocked waiting for audit writes.

**Sensitive field redaction:** `password_hash`, `hashed_key`, and similar fields are automatically stripped from `diff` payloads before storage.

**API:**
- `GET /audit-logs` — paginated log (filterable by `actor`, `action`, `resource_type`, date range). Requires `ADMIN+`.
- `GET /audit-logs/{id}` — full event detail including diff
- `GET /audit-logs/export` — CSV export. Requires `ADMIN+`.

---

### 5.8 Usage Metering

**Real-time tracking:**
- `UsageMeterMiddleware` runs on every authenticated request and executes `INCR usage:{org_id}:{metric}:{hour_bucket}` in Redis
- This is a single atomic Redis command — negligible latency impact

**Persistence:**
- Celery Beat runs an hourly job that reads Redis counters and writes aggregated `UsageRecord` rows to PostgreSQL
- Redis counters are cleared after flush

**Endpoints:**
- `GET /usage/summary` — returns current period API call count vs plan limit. Visible to `VIEWER+`.

**Alerts:**
- When usage reaches 80% of the plan limit: fire a webhook or email notification
- When usage reaches 100% of the plan limit: fire a second alert; plan enforcement grace period begins

---

### 5.9 Feature Flags

**Model:** `FeatureFlag(key, enabled_for_plans, enabled_for_orgs, rollout_pct)`

**Evaluation logic:**
1. Is this flag enabled for the org's current plan?
2. Is this org explicitly in `enabled_for_orgs` (override)?
3. Is this org's numeric hash within the `rollout_pct` bucket?

**Cache layer:** Redis with 60-second TTL. Every evaluation hits Redis first; DB is queried only on cache miss. This means zero meaningful DB overhead at high call rates.

**API:**
- `GET /me/features` — returns a map of `{feature_key: true/false}` for every flag, evaluated for the current org. Used by frontends to show/hide feature-gated UI.

**Admin control:** Superusers can flip flags per-tenant through the Django admin without a deploy.

---

### 5.10 Soft Deletes & Data Versioning

**Soft Deletes:**
- All tenant models inherit `SoftDeleteMixin`
- Deletion sets `deleted_at = now()` and `deleted_by = current_user` instead of executing `DELETE`
- Default queryset `.alive()` excludes soft-deleted records; `.deleted()` shows only deleted
- `POST /resources/{id}/restore` — restores a soft-deleted resource (ADMIN+)

**Data Versioning:**
- `VersionedMixin` increments a `version` integer on every save
- A `ResourceSnapshot` model stores the full JSON payload of the resource after every write
- `GET /resources/{id}/history` — returns the full version timeline (all snapshots)

---

## 6. API Endpoint Summary

| Method | Endpoint | Description | Minimum Role |
|---|---|---|---|
| POST | `/auth/register` | Create org + owner user | PUBLIC |
| POST | `/auth/token` | Login → JWT pair | PUBLIC |
| POST | `/auth/token/refresh` | Refresh access token | PUBLIC |
| POST | `/auth/invite` | Send org invitation | ADMIN+ |
| POST | `/auth/accept-invite/{token}` | Join org via invitation | PUBLIC (signed token) |
| GET | `/me` | Current user + permissions | VIEWER+ |
| GET | `/me/permissions` | Full permission list | VIEWER+ |
| GET | `/me/features` | Feature flags for this tenant | VIEWER+ |
| GET | `/orgs/{id}/members` | List members with roles | VIEWER+ |
| PATCH | `/orgs/{id}/members/{uid}` | Change member role | ADMIN+ |
| DELETE | `/orgs/{id}/members/{uid}` | Remove member | ADMIN+ |
| POST | `/api-keys` | Create API key (secret shown once) | ADMIN+ |
| GET | `/api-keys` | List keys (masked) | VIEWER+ |
| PATCH | `/api-keys/{id}` | Update key name/scopes/expiry | ADMIN+ |
| DELETE | `/api-keys/{id}` | Revoke key immediately | ADMIN+ |
| POST | `/api-keys/{id}/rotate` | Issue new key, expire old in 24h | ADMIN+ |
| GET | `/billing/plans` | List available plans | PUBLIC |
| GET | `/billing/subscription` | Current plan + status | BILLING+ |
| POST | `/billing/subscribe` | Change plan | OWNER |
| GET | `/billing/invoices` | Invoice history | BILLING+ |
| POST | `/billing/webhooks` | Mock Stripe event handler | SIGNED (webhook secret) |
| GET | `/audit-logs` | Paginated event log (filterable) | ADMIN+ |
| GET | `/audit-logs/{id}` | Full event detail with diff | ADMIN+ |
| GET | `/audit-logs/export` | CSV export | ADMIN+ |
| GET | `/usage/summary` | API calls this period vs limit | VIEWER+ |
| GET | `/health` | DB + Redis status | PUBLIC |

---

## 7. Build Phases & Timeline

### Phase 1 — Project Scaffold & Multi-Tenant Foundation
**Week 1 (~3–5 days)**

Set up the entire project skeleton and all foundational tenant infrastructure. Nothing built in later phases should require rethinking the core.

- Initialize Django + DRF + PostgreSQL + JWT (`djangorestframework-simplejwt`)
- Dependency management via Poetry or `uv`
- Docker Compose for: `web`, `db`, `redis`, `celery`
- Environment config via `django-environ`
- Pre-commit hooks: `black`, `isort`, `flake8`, `mypy`
- Custom `AbstractUser` model — no username field; email is the unique identifier
- `Organization` model: `slug`, `plan`, `created_at`, `is_active`
- `OrganizationMembership` model: `user ↔ org` join with `role` enum
- `TenantScopedManager` injecting `filter(organization=get_current_org())` into every queryset
- `SoftDeleteMixin` on all tenant models
- Auth endpoints: `/auth/register`, `/auth/token`, `/auth/token/refresh`, `/auth/invite`, `/auth/accept-invite/{token}`
- JWT customization: embed `org_id` + `role` in token claims
- Middleware for per-request tenant context injection
- Token blocklist for logout/revocation

### Phase 2 — RBAC: Role-Based Access Control
**Week 1–2 (~3–4 days)**

Implement the permission system that gates every route and resource.

- Five-role enum: `OWNER`, `ADMIN`, `MEMBER`, `VIEWER`, `BILLING`
- Static permission registry: `role → set[permission]`
- Custom DRF permission class: `HasTenantPermission`
- `@require_permission("scope:action")` decorator
- Object-level permission for resource ownership
- `GET /me/permissions` endpoint
- Member management endpoints: list, change role, remove
- pytest fixtures: one org, 5 roles, 5 test users
- Role × endpoint matrix test (CI gate)
- Cross-tenant leak test suite

### Phase 3 — API Key System + Rate Limiting
**Week 2 (~3–4 days)**

Implement Stripe-style API keys and Redis-based rate limiting.

- `ApiKey` model: prefix, `hashed_key` (SHA-256 HMAC), `scopes`, `env`, `expires_at`, `last_used_at`, `is_active`, `created_by`
- Full CRUD for API key management
- Key rotation endpoint (24-hour overlap window)
- Custom DRF `Authentication` class for Bearer API key auth
- Redis cache: key-hash → org_id with 60s TTL
- Async `last_used_at` update via Celery
- Lua-script sliding-window rate limiter (atomic)
- Per-plan rate limit tiers (FREE / PRO / ENTERPRISE)
- Standard `X-RateLimit-*` response headers
- HTTP `429` + `Retry-After` on limit exceeded

### Phase 4 — Billing Hooks + Subscription Plans
**Week 3 (~3–4 days)**

Wire up billing models and a mock Stripe-compatible webhook layer.

- `Plan`, `Subscription`, `Invoice`, `UsageRecord` models
- Billing endpoints: plans, subscription, subscribe, invoices, webhooks
- `BillingService` interface (swappable real vs. mock)
- Mock webhook events: `payment_succeeded`, `payment_failed`, `subscription_canceled`
- Celery task: send invoice email on `payment_succeeded`
- Celery Beat: daily usage aggregation
- `check_plan_limit()` helper enforcing hard limits
- 7-day grace period logic
- Feature gate checks per plan

### Phase 5 — Audit Logs + Usage Metering
**Week 3–4 (~3 days)**

Build the immutable event log and real-time usage tracking pipeline.

- `AuditLog` model: actor, org, action, resource_type, resource_id, diff (JSONB), ip_address, user_agent, request_id, created_at (indexed)
- `AuditLogMiddleware` capturing every POST/PATCH/DELETE
- Django signals on key models (User, ApiKey, etc.)
- Async write via Celery (never blocks the request)
- Sensitive field redaction before storage
- Audit log endpoints: list (filterable, paginated), detail, CSV export
- `UsageMeterMiddleware` — Redis INCR per tenant per metric per hour bucket
- Celery Beat: hourly flush of Redis counters to `UsageRecord` DB rows
- `GET /usage/summary` — current period vs plan limit
- Usage alert webhooks / emails at 80% and 100% thresholds

### Phase 6 — Killer Add-ons: Flags, Versioning, Testing, CI/CD
**Week 4–6 (~5–7 days)**

Add the portfolio-differentiating features and productionize everything.

- `FeatureFlag` model: key, enabled_for_plans, enabled_for_orgs, rollout_pct
- `is_feature_enabled(org, key)` helper with Redis cache (60s TTL)
- `GET /me/features` endpoint
- Admin UI toggle per tenant
- `SoftDeleteMixin` + `.alive()` / `.deleted()` queryset managers
- `VersionedMixin` with auto-incrementing version integer
- `ResourceSnapshot` model: full JSON snapshot on every write
- `POST /resources/{id}/restore` and `GET /resources/{id}/history` endpoints
- Full test suite: `pytest` + `pytest-django` + `factory_boy`
- Unit tests: service layer, permission matrix, rate limit Lua logic
- Integration tests: full request → response with real DB
- Cross-tenant isolation suite (100% coverage required, CI gate)
- Load tests: Locust scripts for rate limit behavior under stress
- Docker Compose + GitHub Actions CI pipeline
- Sentry exception tracking
- Structured JSON logging via `structlog`
- `GET /health` endpoint (DB + Redis liveness)
- `drf-spectacular` for OpenAPI 3.0 schema + Swagger UI

---

## 8. Success Criteria

| Metric | Target |
|---|---|
| Cross-tenant isolation test pass rate | 100% — zero exceptions |
| Role × endpoint matrix test pass rate | 100% |
| Rate limit behavior (Locust) | 429 returned within ±5% of configured limit |
| Audit log async write overhead | < 1ms added to request latency (fire-and-forget) |
| API key auth latency (cached) | < 5ms (Redis cache hit) |
| OpenAPI schema completeness | All 25+ endpoints documented |
| CI pipeline duration | < 5 minutes |

---

## 9. Out of Scope (Future Phases)

- Real Stripe API integration (drop-in replacement for mock `BillingService`)
- Frontend dashboard (React / Next.js)
- Schema-per-tenant or database-per-tenant isolation models
- SSO / SAML authentication
- Multi-region deployments
- Self-serve org deletion
- Public API documentation site
