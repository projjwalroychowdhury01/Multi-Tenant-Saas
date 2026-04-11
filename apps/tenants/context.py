"""
Thread-local tenant context.

Every request that is authenticated sets the current organization via
`set_current_org()`.  All TenantManager querysets then call
`get_current_org()` to automatically scope their results.

CRITICAL: The context MUST be cleared in a `finally` block at request end
to prevent thread-reuse from leaking one tenant's context into the next
request on the same OS thread.
"""

import threading

_local = threading.local()


def get_current_org():
    """Return the Organization currently bound to this thread, or None."""
    return getattr(_local, "org", None)


def set_current_org(org):
    """Bind ``org`` to the current thread."""
    _local.org = org


def clear_current_org():
    """Remove the tenant binding from this thread."""
    _local.org = None
