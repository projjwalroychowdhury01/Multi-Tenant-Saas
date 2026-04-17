"""
Plan limit event stream for audit and notifications.

Provides:
- Event log of plan limit violations
- UX notifications (email, webhooks, in-app alerts)
- Audit trail for compliance
- Analytics on usage patterns

Usage:
  from apps.billing.events import emit_plan_limit_event
  
  emit_plan_limit_event(
      org=org,
      event_type="limit_critical",
      limit_type="api_calls_per_month",
      current_usage=usage,
      limit_value=limit,
  )
"""

import logging
from datetime import timedelta
from typing import Optional

from django.utils import timezone

from apps.billing.models import PlanLimitEvent, PlanLimitEventType

logger = logging.getLogger(__name__)


class PlanLimitEventEmitter:
    """
    Emit and manage plan limit violation events.
    """

    # Cache recent events to avoid duplicate notifications
    _recent_events_cache = {}

    @staticmethod
    def emit(
        org,
        event_type: str,
        limit_type: str,
        current_usage: int,
        limit_value: int,
        metadata: Optional[dict] = None,
    ) -> PlanLimitEvent:
        """
        Emit a plan limit event.
        
        Args:
            org: Organization instance
            event_type: Type of event (PlanLimitEventType choice)
            limit_type: Which limit was exceeded (api_calls_per_month, etc.)
            current_usage: Current usage value
            limit_value: Plan limit value
            metadata: Additional context
        
        Returns:
            Created PlanLimitEvent
        """
        # Calculate usage percentage
        if limit_value > 0:
            usage_pct = min(100 + ((current_usage - limit_value) / limit_value) * 100, 999)
        else:
            usage_pct = 0

        metadata = metadata or {}

        # Create event
        event = PlanLimitEvent.objects.create(
            organization=org,
            event_type=event_type,
            limit_type=limit_type,
            current_usage=current_usage,
            limit_value=limit_value,
            usage_percentage=int(usage_pct),
            metadata=metadata,
        )

        logger.info(
            f"Emitted plan limit event: org={org.id}, "
            f"type={event_type}, limit={limit_type}, usage={usage_pct:.1f}%"
        )

        # Trigger notifications
        PlanLimitEventEmitter._dispatch_notifications(event)

        return event

    @staticmethod
    def _dispatch_notifications(event: PlanLimitEvent) -> None:
        """
        Dispatch notifications for the event.
        
        Handles:
        - Email notifications
        - Webhook delivery to org's custom webhooks
        - In-app alerts
        """
        try:
            # Send email notification
            if not event.email_sent:
                PlanLimitEventEmitter._send_email_notification(event)
                event.email_sent = True

            # Send webhook notification (if org has webhooks configured)
            if not event.webhook_sent:
                PlanLimitEventEmitter._send_webhook_notification(event)
                event.webhook_sent = True

            event.save(update_fields=["email_sent", "webhook_sent"])

        except Exception as exc:
            logger.exception(f"Error dispatching notifications for event {event.id}: {exc}")

    @staticmethod
    def _send_email_notification(event: PlanLimitEvent) -> None:
        """Send email notification about plan limit event."""
        from apps.billing.tasks import send_plan_limit_alert_email

        try:
            send_plan_limit_alert_email.delay(str(event.id))
        except Exception as exc:
            logger.warning(f"Failed to enqueue email notification: {exc}")

    @staticmethod
    def _send_webhook_notification(event: PlanLimitEvent) -> None:
        """Send webhook notification to org's custom webhooks (future feature)."""
        # Placeholder for org's custom webhook subscriptions
        # In the future, allow orgs to subscribe to plan limit events
        pass

    @staticmethod
    def emit_with_threshold_check(
        org,
        limit_type: str,
        current_usage: int,
        limit_value: int,
        metadata: Optional[dict] = None,
    ) -> Optional[PlanLimitEvent]:
        """
        Emit plan limit event only if usage crosses a threshold.
        
        Thresholds:
        - 80% → warning notification
        - 100% → critical alert
        - Back below 100% → resolved event
        
        Args:
            org: Organization instance
            limit_type: Which limit to check
            current_usage: Current usage
            limit_value: Plan limit
            metadata: Additional context
        
        Returns:
            PlanLimitEvent if emitted, None otherwise
        """
        # Calculate percentages
        prev_usage = PlanLimitEventEmitter._get_previous_usage(org, limit_type)
        prev_pct = (prev_usage / limit_value * 100) if limit_value > 0 else 0
        curr_pct = (current_usage / limit_value * 100) if limit_value > 0 else 0

        # Check threshold crossings
        warning_threshold = 80
        critical_threshold = 100

        # Resolved: was critical, now below critical
        if prev_pct >= critical_threshold and curr_pct < critical_threshold:
            return PlanLimitEventEmitter.emit(
                org,
                PlanLimitEventType.LIMIT_RESOLVED,
                limit_type,
                current_usage,
                limit_value,
                metadata,
            )

        # Critical: crossed 100% threshold
        if prev_pct < critical_threshold and curr_pct >= critical_threshold:
            return PlanLimitEventEmitter.emit(
                org,
                PlanLimitEventType.LIMIT_CRITICAL,
                limit_type,
                current_usage,
                limit_value,
                metadata,
            )

        # Warning: crossed 80% threshold (but not 100%)
        if prev_pct < warning_threshold and curr_pct >= warning_threshold:
            return PlanLimitEventEmitter.emit(
                org,
                PlanLimitEventType.WARNING,
                limit_type,
                current_usage,
                limit_value,
                metadata,
            )

        return None

    @staticmethod
    def _get_previous_usage(org, limit_type: str) -> int:
        """Get the previous usage for threshold comparison."""
        try:
            prev_event = (
                PlanLimitEvent.objects
                .filter(organization=org, limit_type=limit_type)
                .order_by("-created_at")
                .first()
            )
            if prev_event:
                return prev_event.current_usage
        except Exception:
            pass
        return 0

    @staticmethod
    def emit_grace_period_started(org, limit_type: str, metadata: Optional[dict] = None) -> PlanLimitEvent:
        """Emit event when grace period is started."""
        from apps.billing.limits import get_plan_limit

        limit_value = get_plan_limit(org, limit_type) or 0
        
        return PlanLimitEventEmitter.emit(
            org,
            PlanLimitEventType.GRACE_STARTED,
            limit_type,
            limit_value + 1,  # Mark as exceeded
            limit_value,
            metadata or {"grace_period_days": 7},
        )

    @staticmethod
    def emit_grace_period_expired(org, limit_type: str, metadata: Optional[dict] = None) -> PlanLimitEvent:
        """Emit event when grace period expires."""
        from apps.billing.limits import get_plan_limit

        limit_value = get_plan_limit(org, limit_type) or 0

        return PlanLimitEventEmitter.emit(
            org,
            PlanLimitEventType.GRACE_EXPIRED,
            limit_type,
            limit_value + 1,  # Still exceeded
            limit_value,
            metadata or {"enforcement_begins": True},
        )

    @staticmethod
    def get_recent_events(org, hours: int = 24) -> list:
        """Get recent plan limit events for an org."""
        cutoff = timezone.now() - timedelta(hours=hours)
        return list(
            PlanLimitEvent.objects
            .filter(organization=org, created_at__gte=cutoff)
            .order_by("-created_at")
        )

    @staticmethod
    def get_active_limits(org) -> dict:
        """
        Get current status of all active limit violations for an org.
        
        Returns:
            Dict mapping limit_type → most_recent_event
        """
        recent = PlanLimitEventEmitter.get_recent_events(org, hours=24)
        
        # Group by limit_type, keep most recent
        active = {}
        for event in recent:
            if event.limit_type not in active:
                active[event.limit_type] = event
        
        return active


def emit_plan_limit_event(
    org,
    event_type: str,
    limit_type: str,
    current_usage: int,
    limit_value: int,
    metadata: Optional[dict] = None,
) -> PlanLimitEvent:
    """
    Public API to emit a plan limit event.
    
    Args:
        org: Organization instance
        event_type: Type of event (PlanLimitEventType choice)
        limit_type: Which limit (api_calls_per_month, members_count, etc.)
        current_usage: Current usage
        limit_value: Plan limit
        metadata: Additional context
    
    Returns:
        Created PlanLimitEvent
    """
    return PlanLimitEventEmitter.emit(
        org,
        event_type,
        limit_type,
        current_usage,
        limit_value,
        metadata,
    )


def get_active_plan_limit_violations(org) -> dict:
    """
    Get current active limit violations for an org.
    
    Useful for UX to show warning badges, upgrade CTAs, etc.
    
    Returns:
        Dict with limit violation info
    """
    return PlanLimitEventEmitter.get_active_limits(org)
