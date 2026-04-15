"""
Celery tasks for the billing app.

Tasks
─────
  send_invoice_email        — fire-and-forget email on payment_succeeded
  aggregate_daily_usage     — Celery Beat periodic task; aggregates Redis
                              counters into UsageRecord rows
  notify_usage_threshold    — sends warning (80 %) / critical (100 %) usage
                              alert emails; deduplicated via Redis sentinel keys
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, ignore_result=True)
def send_invoice_email(self, invoice_id: str) -> None:
    """
    Send an invoice confirmation email after a successful payment.

    Uses Django's email backend (console in dev, SMTP/SES in prod).
    Fire-and-forget — the webhook handler returns immediately; this
    task runs asynchronously in the Celery worker.
    """
    try:
        from apps.billing.models import Invoice
        from django.core.mail import send_mail
        from django.conf import settings

        invoice = Invoice.objects.select_related(
            "subscription__organization",
            "subscription__plan",
        ).get(id=invoice_id)

        org = invoice.subscription.organization
        plan = invoice.subscription.plan
        amount = f"${invoice.amount_cents / 100:.2f}"

        # In production: send to the org owner's email.
        # For now we log the email content (console backend).
        subject = f"Payment received — {plan.name} plan"
        message = (
            f"Hi {org.name},\n\n"
            f"Thank you! We received your payment of {amount} "
            f"for the {plan.name} plan.\n\n"
            f"Invoice reference: {invoice.stripe_invoice_id}\n"
            f"Period: {invoice.period_start:%Y-%m-%d} → {invoice.period_end:%Y-%m-%d}\n\n"
            f"The {org.name} team"
        )

        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[f"owner@{org.slug}.example.com"],
                fail_silently=True,
            )
            logger.info("send_invoice_email: sent for invoice %s", invoice_id)
        except Exception as mail_exc:
            logger.warning("send_invoice_email: mail failed for invoice %s: %s", invoice_id, mail_exc)

    except Exception as exc:
        logger.error("send_invoice_email failed for invoice %s: %s", invoice_id, exc)
        raise self.retry(exc=exc)


@shared_task(bind=True, ignore_result=True)
def aggregate_daily_usage(self) -> None:
    """
    Flush Redis usage counters to UsageRecord rows in PostgreSQL.

    Celery Beat runs this task hourly (configured in CELERY_BEAT_SCHEDULE).

    Key pattern in Redis: ``usage:{org_id}:api_calls:{hour_bucket}``
    where ``hour_bucket`` is ``YYYY-MM-DD-HH`` in UTC.

    Flush strategy:
    1. Scan all matching keys
    2. For each key: read the value, write a UsageRecord, delete the key
    3. Use Redis GETDEL (atomically read+delete) to avoid double-counting

    In Phase 5 the UsageMeterMiddleware will populate these Redis keys.
    This task is defined here (Phase 4) so the periodic schedule is in
    place before the middleware that fills those keys is written.
    """
    try:
        from django.core.cache import cache

        redis_client = cache._cache.get_client()
        pattern = "usage:*:api_calls:*"
        cursor = 0
        flushed = 0

        while True:
            cursor, keys = redis_client.scan(cursor, match=pattern, count=100)
            for key in keys:
                _flush_usage_key(redis_client, key.decode())
                flushed += 1
            if cursor == 0:
                break

        logger.info("aggregate_daily_usage: flushed %d Redis usage keys", flushed)

    except Exception as exc:
        logger.error("aggregate_daily_usage failed: %s", exc, exc_info=True)
        # Don't retry — next scheduled run will pick up any missed keys


def _flush_usage_key(redis_client, key: str) -> None:
    """
    Read the counter at *key*, write a UsageRecord, and delete the key.

    Key format: ``usage:{org_id}:{metric}:{YYYY-MM-DD-HH}``
    """
    import uuid
    from datetime import datetime, timezone as dt_tz

    from apps.billing.models import UsageRecord

    try:
        # Atomically read and delete — avoids a window where the counter
        # is incremented between reading and deleting.
        quantity = redis_client.getdel(key)
        if quantity is None:
            return
        quantity = int(quantity)
        if quantity == 0:
            return

        # Parse: usage:<org_id>:<metric>:<YYYY-MM-DD-HH>
        parts = key.split(":")
        if len(parts) != 4:
            logger.warning("_flush_usage_key: unexpected key format %r", key)
            return

        _, org_id, metric_name, hour_bucket = parts
        period_start = datetime.strptime(hour_bucket, "%Y-%m-%d-%H").replace(
            tzinfo=dt_tz.utc
        )
        from datetime import timedelta
        period_end = period_start + timedelta(hours=1)

        from apps.tenants.models import Organization
        try:
            org = Organization.all_objects.get(id=org_id)
        except Organization.DoesNotExist:
            logger.warning("_flush_usage_key: org %s not found, skipping key %r", org_id, key)
            return

        UsageRecord.objects.create(
            organization=org,
            metric_name=metric_name,
            quantity=quantity,
            period_start=period_start,
            period_end=period_end,
        )
        logger.debug(
            "_flush_usage_key: %s → org=%s metric=%s qty=%d",
            key, org_id, metric_name, quantity,
        )

    except Exception as exc:
        logger.error("_flush_usage_key failed for key %r: %s", key, exc)


# ── notify_usage_threshold ──────────────────────────────────────────────────────


@shared_task(bind=True, max_retries=2, default_retry_delay=30, ignore_result=True)
def notify_usage_threshold(
    self,
    org_id: str,
    limit_type: str,
    usage: int,
    limit: int,
    severity: str,
) -> None:
    """
    Send a usage-threshold alert email when an org crosses the 80 % or 100 %
    consumption boundary for a given *limit_type*.

    Deduplication
    ─────────────
    A Redis sentinel key ``usage_alert:{org_id}:{limit_type}:{severity}:{YYYY-MM}``
    is set atomically with ``SET NX`` (set-if-not-exists).  If the key already
    exists (alert already sent this month), the task returns immediately without
    sending another email.  The key TTL is 32 days so it expires automatically
    after the billing period rolls over.

    Severity levels:
    - ``warning``  — 80 % consumed; prompts the org to consider upgrading.
    - ``critical`` — 100 % consumed; grace period is now open; hard block
                     is imminent if no upgrade is made.

    Args:
        org_id:     String UUID of the organization.
        limit_type: One of ``members_count``, ``api_calls_per_month``, etc.
        usage:      Current usage value at alert-fire time.
        limit:      Plan limit value.
        severity:   ``"warning"`` or ``"critical"``.
    """
    from django.conf import settings
    from django.core.cache import cache
    from django.core.mail import send_mail
    from django.utils.timezone import now

    try:
        from apps.tenants.models import Organization

        try:
            org = Organization.all_objects.select_related("billing_plan").get(id=org_id)
        except Organization.DoesNotExist:
            logger.warning("notify_usage_threshold: org %s not found", org_id)
            return

        # ── Deduplication via Redis NX key ─────────────────────────────────────
        month_bucket = now().strftime("%Y-%m")
        sentinel_key = f"usage_alert:{org_id}:{limit_type}:{severity}:{month_bucket}"

        # cache.add() uses SET NX semantics — returns True only if the key was
        # absent (i.e., we are the first to fire this alert this month).
        # TTL: 32 days so the key outlasts any billing month.
        already_sent = not cache.add(sentinel_key, "1", timeout=60 * 60 * 24 * 32)
        if already_sent:
            logger.debug(
                "notify_usage_threshold: alert %s already sent this month, skipping.",
                sentinel_key,
            )
            return

        # ── Compose the email ──────────────────────────────────────────────────
        pct = int((usage / limit) * 100) if limit else 0
        plan_name = org.billing_plan.name if org.billing_plan else "Free"

        if severity == "critical":
            subject = f"⚠️ URGENT: {org.name} has reached 100 % of its {limit_type} limit"
            message = (
                f"Hi {org.name},\n\n"
                f"Your organisation has reached {pct} % ({usage}/{limit}) of its "
                f"{limit_type} quota on the {plan_name} plan.\n\n"
                f"A 7-day grace period is now active. If you do not upgrade before "
                f"this period ends, new requests that exceed the limit will be blocked.\n\n"
                f"To avoid service interruption, please upgrade your plan immediately:\n"
                f"  POST /billing/subscribe\n\n"
                f"– The {org.name} platform"
            )
        else:  # warning
            subject = f"Heads up: {org.name} is at {pct} % of its {limit_type} limit"
            message = (
                f"Hi {org.name},\n\n"
                f"Your organisation has used {pct} % ({usage}/{limit}) of its "
                f"{limit_type} quota on the {plan_name} plan.\n\n"
                f"Consider upgrading before you hit the limit to avoid service interruption:\n"
                f"  POST /billing/subscribe\n\n"
                f"– The {org.name} platform"
            )

        # ── Send the email ─────────────────────────────────────────────────────
        # In production, resolve the org owner's real email address.
        # The console email backend used in dev/test will print the output.
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[f"owner@{org.slug}.example.com"],
                fail_silently=True,
            )
            logger.info(
                "notify_usage_threshold: sent %s alert for org %s %s (%d/%d)",
                severity, org_id, limit_type, usage, limit,
            )
        except Exception as mail_exc:
            logger.warning(
                "notify_usage_threshold: mail send failed for org %s: %s",
                org_id, mail_exc,
            )

    except Exception as exc:
        logger.error(
            "notify_usage_threshold failed for org %s %s: %s",
            org_id, limit_type, exc,
        )
        raise self.retry(exc=exc)

