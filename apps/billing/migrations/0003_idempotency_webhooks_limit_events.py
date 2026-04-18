"""
Migration for idempotency, webhook events, and plan limit events.

Adds:
- IdempotencyKey: Stores results of idempotent operations for replay protection
- WebhookEvent: Records all webhook events for audit trail and replay detection
- PlanLimitEvent: Event stream for plan limit violations and notifications
"""

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0002_seed_plans"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        # ── IdempotencyKey ────────────────────────────────────────────────
        migrations.CreateModel(
            name="IdempotencyKey",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("idempotency_key", models.CharField(db_index=True, max_length=255)),
                ("operation_type", models.CharField(db_index=True, max_length=100)),
                ("request_hash", models.CharField(max_length=64)),
                ("response_status", models.IntegerField()),
                ("response_data", models.JSONField()),
                ("error_message", models.TextField(blank=True, null=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="idempotency_keys",
                        to="tenants.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "Idempotency Key",
                "verbose_name_plural": "Idempotency Keys",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="idempotencykey",
            constraint=models.UniqueConstraint(
                fields=["organization", "idempotency_key"], name="unique_org_idempotency_key"
            ),
        ),
        migrations.AddIndex(
            model_name="idempotencykey",
            index=models.Index(
                fields=["organization", "idempotency_key"], name="billing_ide_org_id_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="idempotencykey",
            index=models.Index(fields=["created_at"], name="billing_ide_created_idx"),
        ),
        # ── WebhookEvent ──────────────────────────────────────────────────
        migrations.CreateModel(
            name="WebhookEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("event_id", models.CharField(db_index=True, max_length=255, unique=True)),
                ("event_type", models.CharField(db_index=True, max_length=100)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processed", "Processed"),
                            ("failed", "Failed"),
                            ("dead_letter", "Dead Letter"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("payload", models.JSONField()),
                ("signature", models.CharField(max_length=255)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True, null=True)),
                ("retry_count", models.PositiveIntegerField(default=0)),
                ("dead_letter_reason", models.TextField(blank=True, null=True)),
                (
                    "organization",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="webhook_events",
                        to="tenants.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "Webhook Event",
                "verbose_name_plural": "Webhook Events",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="webhookevent",
            index=models.Index(fields=["event_type", "status"], name="billing_web_event_type_idx"),
        ),
        migrations.AddIndex(
            model_name="webhookevent",
            index=models.Index(fields=["status", "created_at"], name="billing_web_status_idx"),
        ),
        migrations.AddIndex(
            model_name="webhookevent",
            index=models.Index(fields=["organization", "created_at"], name="billing_web_org_idx"),
        ),
        # ── PlanLimitEvent ────────────────────────────────────────────────
        migrations.CreateModel(
            name="PlanLimitEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("limit_warning", "Limit Warning (80%)"),
                            ("limit_critical", "Limit Critical (100%)"),
                            ("grace_started", "Grace Period Started"),
                            ("grace_expired", "Grace Period Expired"),
                            ("limit_resolved", "Limit Resolved (Back Under)"),
                        ],
                        db_index=True,
                        max_length=20,
                    ),
                ),
                ("limit_type", models.CharField(db_index=True, max_length=100)),
                ("current_usage", models.PositiveBigIntegerField()),
                ("limit_value", models.PositiveBigIntegerField()),
                ("usage_percentage", models.PositiveSmallIntegerField()),
                ("metadata", models.JSONField(default=dict)),
                ("email_sent", models.BooleanField(default=False)),
                ("webhook_sent", models.BooleanField(default=False)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="plan_limit_events",
                        to="tenants.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "Plan Limit Event",
                "verbose_name_plural": "Plan Limit Events",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="planlimitevent",
            index=models.Index(fields=["organization", "event_type"], name="billing_plan_org_idx"),
        ),
        migrations.AddIndex(
            model_name="planlimitevent",
            index=models.Index(fields=["event_type", "created_at"], name="billing_plan_event_idx"),
        ),
        migrations.AddIndex(
            model_name="planlimitevent",
            index=models.Index(
                fields=["organization", "created_at"], name="billing_plan_created_idx"
            ),
        ),
    ]
