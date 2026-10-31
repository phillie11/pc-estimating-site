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


# ---------------------------------------------------------------------------
# Per-request memo
# ---------------------------------------------------------------------------
# Lookups many parts of one page repeat (which companies a subcontractor represents on each
# project, the jobs a PM runs) are worked out once per request. TenantMiddleware opens the memo
# for each request and closes it afterwards; outside a request (jobs, commands, tests calling a
# function directly) nothing is remembered. Any write to the database during the request clears it
# (clear_memo_on_write, wrapped round every query by TenantMiddleware), so a request that changes
# something never reads a value from before the change. That covers queryset.update() and bulk writes
# too, which model signals miss. Signals are deliberately not used: a pre/post_delete receiver on every
# model switches off Django's fast delete, and on MySQL the slow path nulls a nullable CASCADE foreign
# key before deleting the row, which breaks check constraints such as viewer_pin_one_record.

_request_memo = contextvars.ContextVar('tenancy_request_memo', default=None)


def request_memo(key, compute):
    """compute(), remembered for the rest of this request under key (a tuple naming what it is)."""
    memo = _request_memo.get()
    if memo is None:
        return compute()
    org = _current_org.get()
    key = (getattr(org, 'pk', None), *key)
    if key not in memo:
        memo[key] = compute()
    return memo[key]


def open_request_memo():
    return _request_memo.set({})


def close_request_memo(token):
    _request_memo.reset(token)


def clear_request_memo(**kwargs):
    memo = _request_memo.get()
    if memo:
        memo.clear()


_WRITES = ('INSERT', 'UPDATE', 'DELETE', 'REPLACE')


def clear_memo_on_write(execute, sql, params, many, context):
    """connection.execute_wrapper hook: forget the memo before any statement that writes."""
    if _request_memo.get() and sql.lstrip()[:7].upper().startswith(_WRITES):
        _request_memo.get().clear()
    return execute(sql, params, many, context)
