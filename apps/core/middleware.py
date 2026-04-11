"""
Core middleware.

RequestIdMiddleware — injects a unique X-Request-ID UUID into every
request so that log lines, audit entries, and Sentry events can all be
correlated back to a single HTTP transaction.
"""

import uuid

from django.utils.deprecation import MiddlewareMixin


class RequestIdMiddleware(MiddlewareMixin):
    """
    Generates a UUID4 request ID and attaches it as:
      - request.request_id  (available to views)
      - X-Request-ID response header (visible to clients / load balancers)
    """

    HEADER = "HTTP_X_REQUEST_ID"
    RESPONSE_HEADER = "X-Request-ID"

    def process_request(self, request):
        # Honour a pre-existing request-id forwarded by an upstream proxy
        request_id = request.META.get(self.HEADER) or str(uuid.uuid4())
        request.request_id = request_id

    def process_response(self, request, response):
        request_id = getattr(request, "request_id", str(uuid.uuid4()))
        response[self.RESPONSE_HEADER] = request_id
        return response
