"""
The current organisation for this request.

Set by TenantMiddleware for every /<org_slug>/ URL and reset afterwards.
TenantManager reads it so tenant models filter themselves automatically:
a view that forgets to filter by organisation still cannot read another
company's rows. Explicit filtering in views stays the primary control; this
is the safety net.
"""
import contextvars
from contextlib import contextmanager

_current_org = contextvars.ContextVar('current_org', default=None)


def get_current_org():
    return _current_org.get()


def set_current_org(org):
    return _current_org.set(org)


def reset_current_org(token):
    _current_org.reset(token)


@contextmanager
def org_context(org):
    """Use in management commands, tasks and tests."""
    token = _current_org.set(org)
    try:
        yield org
    finally:
        _current_org.reset(token)
