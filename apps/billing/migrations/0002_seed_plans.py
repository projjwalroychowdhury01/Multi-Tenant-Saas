"""
Data migration: seed default billing plans (FREE / PRO / ENTERPRISE).

These are the canonical plan tiers used by the rate limiter and billing
enforcement logic.  They must exist before any org can subscribe to a plan.

Running this migration again on an already-seeded database is safe — we
use get_or_create to avoid duplicate rows.
"""

from django.db import migrations


FREE_PLAN = {
    "name": "Free",
    "slug": "free",
    "price_monthly": "0.00",
    "limits": {
        "members_count": 3,
        "api_calls_per_month": 10_000,
        "storage_mb": 512,
    },
    "features": {
        "audit_logs": False,
        "feature_flags": False,
        "sso": False,
    },
    "is_active": True,
}

PRO_PLAN = {
    "name": "Pro",
    "slug": "pro",
    "price_monthly": "49.00",
    "limits": {
        "members_count": 25,
        "api_calls_per_month": 500_000,
        "storage_mb": 20_480,
    },
    "features": {
        "audit_logs": True,
        "feature_flags": False,
        "sso": False,
    },
    "is_active": True,
}

ENTERPRISE_PLAN = {
    "name": "Enterprise",
    "slug": "enterprise",
    "price_monthly": "299.00",
    "limits": {
        "members_count": 500,
        "api_calls_per_month": 10_000_000,
        "storage_mb": 512_000,
    },
    "features": {
        "audit_logs": True,
        "feature_flags": True,
        "sso": True,
    },
    "is_active": True,
}


def seed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for plan_data in (FREE_PLAN, PRO_PLAN, ENTERPRISE_PLAN):
        Plan.objects.get_or_create(
            slug=plan_data["slug"],
            defaults=plan_data,
        )


def unseed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(slug__in=["free", "pro", "enterprise"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_plans, reverse_code=unseed_plans),
    ]
