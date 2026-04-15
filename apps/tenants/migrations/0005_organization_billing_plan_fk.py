"""
Phase 4 migration: replace Organization.plan (CharField) with
Organization.billing_plan (ForeignKey → billing.Plan).

Strategy
────────
1. Add the new `billing_plan` FK column (null=True so existing rows are
   unaffected — they simply start unsubscribed).
2. Remove the old `plan` CharField.

Data migration is intentionally omitted:
- Existing orgs had `plan = "FREE"` which was a sentinel, not a real Plan
  row reference.  The canonical source of truth is the Subscription model.
  Any code that needs a plan should look through org.subscription.plan.
- The `billing_plan` FK on Organization is a *denormalization* for fast
  rate-limit lookups only; it is kept in sync by BillingService.subscribe().
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        # billing.Plan must exist before we can FK to it
        ("billing", "0002_seed_plans"),
        ("tenants", "0004_initial"),
    ]

    operations = [
        # Step 1: add the nullable FK column
        migrations.AddField(
            model_name="organization",
            name="billing_plan",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Active plan tier. Managed via the Subscription model; "
                    "do not set directly."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="organizations",
                to="billing.plan",
            ),
        ),
        # Step 2: drop the old CharField
        migrations.RemoveField(
            model_name="organization",
            name="plan",
        ),
    ]
