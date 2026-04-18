"""URL patterns for the api_keys app."""

from django.urls import path

from apps.api_keys.views import api_key_detail, api_key_list_create, api_key_rotate

app_name = "api_keys"

urlpatterns = [
    path("", api_key_list_create, name="list-create"),
    path("<uuid:key_id>/", api_key_detail, name="detail"),
    path("<uuid:key_id>/rotate/", api_key_rotate, name="rotate"),
]
