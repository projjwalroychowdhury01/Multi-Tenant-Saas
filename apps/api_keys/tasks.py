"""
Celery tasks for the api_keys app.

Tasks
─────
  update_api_key_last_used — async bump of last_used_at to avoid blocking
                             the request thread with a DB write on every hit.
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=5, ignore_result=True)
def update_api_key_last_used(self, key_id: str) -> None:
    """
    Update the `last_used_at` field for the given API key.

    This is intentionally fire-and-forget — the request has already returned
    a response before this task executes.  Use `all_objects` to bypass the
    TenantManager filter (tasks operate across all tenants).
    """
    try:
        from apps.api_keys.models import ApiKey

        updated = ApiKey.all_objects.filter(id=key_id).update(last_used_at=timezone.now())
        if not updated:
            logger.warning("update_api_key_last_used: key %s not found", key_id)
    except Exception as exc:
        logger.error("update_api_key_last_used failed for key %s: %s", key_id, exc)
        raise self.retry(exc=exc)
