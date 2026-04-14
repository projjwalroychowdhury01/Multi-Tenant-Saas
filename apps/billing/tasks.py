"""
Celery tasks for the billing app.

Tasks
─────
  send_invoice_email      — fire-and-forget email on payment_succeeded
  aggregate_daily_usage   — Celery Beat periodic task; aggregates Redis
                            counters into UsageRecord rows
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
