"""
Custom DRF exception handler.

Normalises all API error responses to a consistent shape:
  {
    "error":   "human-readable message",
    "detail":  "...",          # optional extra detail
    "code":    "error_code",   # machine-readable
  }
"""

from rest_framework.views import exception_handler


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)

    if response is not None:
        data = response.data

        # Flatten DRF's default {"detail": ...} into our envelope
        if isinstance(data, dict) and "detail" in data:
            response.data = {
                "error": str(data["detail"]),
                "code": getattr(data["detail"], "code", "error"),
            }
        else:
            response.data = {
                "error": "An error occurred.",
                "detail": data,
                "code": "error",
            }

    return response
