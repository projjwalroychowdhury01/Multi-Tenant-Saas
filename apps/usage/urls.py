"""URL patterns for the usage app — mounted at /usage/ in config/urls.py."""

from django.urls import path

from apps.usage.views import get_usage_summary

app_name = "usage"

urlpatterns = [
    path("summary/", get_usage_summary, name="summary"),
]
