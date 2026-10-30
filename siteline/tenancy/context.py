"""
The current organisation for this request.

Set by TenantMiddleware for every /<org_slug>/ URL and reset afterwards.
TenantManager reads it so tenant models filter themselves automatically:
a view that forgets to filter by organisation still cannot read another
company's rows. Explicit filtering in views stays the primary control; this
is the safety net.

It fails closed: running a TenantModel.objects query with no organisation set
raises TenantContextMissing instead of returning every company's rows. Code
that runs outside a request (commands, webhooks, admin, the shell) either sets
one with org_context(org) or says it means every company with unscoped().
"""
import contextvars
from contextlib import contextmanager

_current_org = contextvars.ContextVar('current_org', default=None)
_unscoped = contextvars.ContextVar('tenancy_unscoped', default=False)


class TenantContextMissing(RuntimeError):
    """A tenant query ran with no current organisation and outside unscoped()."""


def get_current_org():
    return _current_org.get()


def set_current_org(org):
    return _current_org.set(org)


def reset_current_org(token):
    _current_org.reset(token)


def is_unscoped():
    return _unscoped.get()


@contextmanager
def org_context(org):
    """Use in management commands, tasks and tests. Wins over an enclosing unscoped()."""
    token, flag = _current_org.set(org), _unscoped.set(False)
    try:
        yield org
    finally:
        _unscoped.reset(flag)
        _current_org.reset(token)


@contextmanager
def unscoped():
    """
    Deliberately work across every organisation: TenantModel.objects behaves
    like all_objects inside the block, and save() needs an explicit
    organisation. Platform admin and cross-company jobs only; prefer looping
    over organisations with org_context(). Keep it greppable: `unscoped()`.
    """
    token, flag = _current_org.set(None), _unscoped.set(True)
    try:
        yield
    finally:
        _unscoped.reset(flag)
        _current_org.reset(token)
