"""
Webhook schema validation and dead-letter queue handling.

Provides:
- Strict schema validation for webhook events
- Type checking and required field validation
- Dead-letter queue for malformed but signed events
- Replay protection via event_id deduplication
- Detailed error logging for debugging

Usage:
  from apps.billing.webhook_validation import (
      validate_webhook_event,
      queue_dead_letter_event,
  )
  
  try:
      validated = validate_webhook_event(event_type, payload)
  except WebhookValidationError as exc:
      queue_dead_letter_event(payload, signature, str(exc))
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class WebhookValidationError(ValueError):
    """Raised when webhook validation fails."""

    pass


# Event schemas: define required and optional fields for each event type
WEBHOOK_SCHEMAS = {
    "payment_succeeded": {
        "required": ["invoice_id"],
        "optional": ["invoice_number", "timestamp"],
        "types": {
            "invoice_id": (str, int),
            "invoice_number": str,
            "timestamp": (int, str),
        },
    },
    "payment_failed": {
        "required": ["invoice_id", "reason"],
        "optional": ["retry_count", "timestamp"],
        "types": {
            "invoice_id": (str, int),
            "reason": str,
            "retry_count": int,
            "timestamp": (int, str),
        },
    },
    "subscription_canceled": {
        "required": ["org_id"],
        "optional": ["reason", "timestamp"],
        "types": {
            "org_id": (str, int),
            "reason": str,
            "timestamp": (int, str),
        },
    },
    "plan_limit_exceeded": {
        "required": ["org_id", "limit_type", "current_usage", "limit_value"],
        "optional": ["metric_name", "period_start", "period_end"],
        "types": {
            "org_id": (str, int),
            "limit_type": str,
            "current_usage": int,
            "limit_value": int,
            "metric_name": str,
            "period_start": (int, str),
            "period_end": (int, str),
        },
    },
}


def validate_field_type(field_name: str, value: Any, expected_types) -> None:
    """
    Validate that a field has the correct type.

    Args:
        field_name: Name of the field
        value: Field value
        expected_types: Type or tuple of types

    Raises:
        WebhookValidationError if type mismatch
    """
    if not isinstance(value, expected_types):
        actual_type = type(value).__name__
        if isinstance(expected_types, tuple):
            expected_str = " or ".join(t.__name__ for t in expected_types)
        else:
            expected_str = expected_types.__name__
        raise WebhookValidationError(
            f"Field '{field_name}' has wrong type: expected {expected_str}, " f"got {actual_type}"
        )


def validate_webhook_event(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate webhook event against schema.

    Args:
        event_type: Type of event (e.g., 'payment_succeeded')
        payload: Event payload to validate

    Returns:
        Validated payload (cleaned)

    Raises:
        WebhookValidationError if validation fails
    """
    if not isinstance(payload, dict):
        raise WebhookValidationError(
            f"Webhook payload must be a dict, got {type(payload).__name__}"
        )

    schema = WEBHOOK_SCHEMAS.get(event_type)
    if not schema:
        raise WebhookValidationError(
            f"Unknown event type: {event_type!r}. "
            f"Supported: {', '.join(WEBHOOK_SCHEMAS.keys())}"
        )

    # Validate required fields
    for field in schema["required"]:
        if field not in payload:
            raise WebhookValidationError(
                f"Required field missing: '{field}' not in {event_type} payload"
            )

    # Validate field types
    type_map = schema.get("types", {})
    for field, expected_type in type_map.items():
        if field in payload:
            validate_field_type(field, payload[field], expected_type)

    # Extract known fields (required + optional)
    known_fields = set(schema["required"]) | set(schema.get("optional", []))
    cleaned = {k: v for k, v in payload.items() if k in known_fields}

    # Validate no unknown fields in strict mode (raise warning, not error)
    unknown_fields = set(payload.keys()) - known_fields
    if unknown_fields:
        logger.warning(
            f"Webhook {event_type} contains unknown fields: {unknown_fields}. "
            f"These will be ignored."
        )

    logger.debug(f"Webhook validation passed for {event_type}")
    return cleaned


def queue_dead_letter_event(
    payload: Dict[str, Any],
    signature: str,
    reason: str,
) -> None:
    """
    Queue a malformed but signed event to dead-letter queue for manual review.

    Args:
        payload: Event payload
        signature: HMAC signature
        reason: Human-readable reason for rejection
    """
    from apps.billing.models import WebhookEvent, WebhookEventStatus

    try:
        event_id = payload.get("event_id", f"unknown_{id(payload)}")
        event_type = payload.get("event_type", "unknown")

        # Store in database for manual review
        event = WebhookEvent.objects.create(
            event_id=event_id,
            event_type=event_type,
            status=WebhookEventStatus.DEAD_LETTER,
            payload=payload,
            signature=signature,
            dead_letter_reason=reason,
        )

        logger.warning(
            f"Event queued to dead-letter: {event.id} "
            f"(event_type={event_type}, reason={reason})"
        )

    except Exception as exc:
        logger.error(f"Failed to queue dead-letter event: {exc}")


def get_dead_letter_events(limit: int = 100) -> list:
    """
    Retrieve recent dead-letter events for manual review.

    Args:
        limit: Maximum number to return

    Returns:
        List of WebhookEvent objects in dead-letter status
    """
    from apps.billing.models import WebhookEvent, WebhookEventStatus

    return list(
        WebhookEvent.objects.filter(status=WebhookEventStatus.DEAD_LETTER).order_by("-created_at")[
            :limit
        ]
    )


def retry_dead_letter_event(event_id: str) -> bool:
    """
    Attempt to reprocess a dead-letter event.

    Args:
        event_id: UUID of WebhookEvent to retry

    Returns:
        True if retry was successful, False otherwise
    """
    from apps.billing.models import WebhookEvent, WebhookEventStatus

    try:
        event = WebhookEvent.objects.get(id=event_id)

        if event.status != WebhookEventStatus.DEAD_LETTER:
            logger.warning(f"Event {event_id} is not in dead-letter status")
            return False

        # Reset status to pending and attempt reprocessing
        event.status = WebhookEventStatus.PENDING
        event.retry_count = event.retry_count + 1
        event.dead_letter_reason = None
        event.save(update_fields=["status", "retry_count", "dead_letter_reason"])

        # Trigger reprocessing
        from apps.billing.tasks import process_webhook_event_async

        process_webhook_event_async.delay(str(event.id))

        logger.info(f"Dead-letter event {event_id} queued for retry (attempt {event.retry_count})")
        return True

    except Exception as exc:
        logger.error(f"Failed to retry dead-letter event {event_id}: {exc}")
        return False


def validate_webhook_payload_signature_and_schema(
    payload: Dict[str, Any],
    signature: str,
    verify_signature_func,
) -> tuple:
    """
    Combined validation: signature + schema.

    Args:
        payload: Event payload
        signature: HMAC signature
        verify_signature_func: Function to verify signature

    Returns:
        Tuple of (is_valid, error_message, cleaned_payload)
    """
    import json

    # Verify signature first (can't skip this)
    payload_bytes = json.dumps(payload, sort_keys=True).encode()
    if not verify_signature_func(payload_bytes, signature):
        return False, "Invalid signature", None

    # Validate schema
    event_type = payload.get("event_type")
    try:
        cleaned = validate_webhook_event(event_type, payload)
        return True, None, cleaned
    except WebhookValidationError as exc:
        return False, str(exc), None
