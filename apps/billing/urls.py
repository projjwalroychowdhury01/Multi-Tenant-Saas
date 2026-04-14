"""URL patterns for the billing app — mounted at /billing/ in config/urls.py."""

from django.urls import path

from apps.billing.views import (
    get_subscription,
    list_invoices,
    list_plans,
    subscribe,
    webhook_handler,
)

app_name = "billing"

urlpatterns = [
    path("plans/", list_plans, name="plans"),
    path("subscription/", get_subscription, name="subscription"),
    path("subscribe/", subscribe, name="subscribe"),
    path("invoices/", list_invoices, name="invoices"),
    path("webhooks/", webhook_handler, name="webhooks"),
]
