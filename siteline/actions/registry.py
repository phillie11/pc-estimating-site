"""
The "Needs me" registry: one list of everything waiting on a person, whatever app it lives in.

A provider is any object with:

    kind          short code, e.g. 'rfi', 'client_variation' (unique)
    label         plain words for a filter chip, e.g. 'Variations to approve'
    covers        record types (RecordIndex.record_type, e.g. 'variations.variation') this
                  provider already lists, so the record-index provider leaves them out
    items(request) -> iterable of Item

`request` needs `user`, `org`, `membership` (and nothing else), so the daily digest can
pass a stand-in built by `request_for()`. Each provider must apply the same access rules
as the view its items link to (visible_projects / portal_projects, sections, archived).

    import actions
    actions.register(MyProvider())
    actions.for_user(request)           # [Item, ...], most urgent first
    actions.grouped(items)              # [('overdue', 'Overdue', [...]), ('today', ...), ...]
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.utils import timezone

PRIORITY_HIGH, PRIORITY_NORMAL = 'high', 'normal'

GROUPS = (
    ('overdue', 'Overdue'),
    ('today', 'Today'),
    ('week', 'This week'),
    ('later', 'Later or no date'),
)


@dataclass
class Item:
    kind: str
    title: str
    url: str
    ref: str = ''
    project: object = None
    due: date | None = None
    priority: str = PRIORITY_NORMAL
    # What the person has to do, in plain words: "Approve or reject", "Price this package"
    action: str = ''
    status_label: str = ''
    tone: str = 'accent'
    amount: Decimal | None = None
    # Who or what it came from: "Northbank Joinery", "Raised by Sam"
    detail: str = ''
    key: str = ''
    kind_label: str = field(default='', compare=False)

    @property
    def is_overdue(self):
        return self.due is not None and self.due < timezone.localdate()

    @property
    def group(self):
        return group_for(self.due)

    @property
    def project_name(self):
        return getattr(self.project, 'name', '')


def group_for(due, today=None):
    """Overdue / Today / This week (the next six days) / Later (further off, or no date)."""
    today = today or timezone.localdate()
    if due is None:
        return 'later'
    if due < today:
        return 'overdue'
    if due == today:
        return 'today'
    if due <= today + timedelta(days=6):
        return 'week'
    return 'later'


_providers = {}


def register(provider):
    """Add a provider. Registering the same kind again replaces it (tests, reloads)."""
    for attr in ('kind', 'label', 'items'):
        if not hasattr(provider, attr):
            raise TypeError(f'A needs-me provider must have `{attr}`.')
    _providers[provider.kind] = provider
    return provider


def unregister(kind):
    _providers.pop(kind, None)


def providers():
    return list(_providers.values())


def covered_record_types():
    return {rt for p in _providers.values() for rt in getattr(p, 'covers', ())}


def kinds():
    return [(p.kind, p.label) for p in _providers.values()]


def _sort_key(item):
    return (item.due is None, item.due or date.max, item.priority != PRIORITY_HIGH, item.ref, item.title)


def for_user(request, project=None, kind=None):
    """
    Everything waiting on request.user in the current organisation, soonest due first
    (high priority first within a day; undated last). Cached on the request, so the bell,
    the page and the dashboard share one set of queries.
    """
    if not getattr(getattr(request, 'user', None), 'is_authenticated', False) or getattr(request, 'membership', None) is None:
        return []
    cache = getattr(request, '_needs_me', None)
    if cache is None:
        cache = []
        for p in _providers.values():
            for item in p.items(request):
                item.kind = item.kind or p.kind
                item.kind_label = item.kind_label or p.label
                cache.append(item)
        cache.sort(key=_sort_key)
        try:
            request._needs_me = cache
        except AttributeError:  # pragma: no cover - a request that won't take attributes
            pass
    items = cache
    if project is not None:
        pk = getattr(project, 'pk', project)
        items = [i for i in items if getattr(i.project, 'pk', None) == pk]
    if kind:
        items = [i for i in items if i.kind == kind]
    return items


def count_for_user(request):
    return len(for_user(request))


def grouped(items, include_empty=False):
    """[(code, label, [items])] in Overdue / Today / This week / Later order."""
    today = timezone.localdate()
    buckets = {code: [] for code, _ in GROUPS}
    for item in items:
        buckets[group_for(item.due, today)].append(item)
    return [(code, label, buckets[code]) for code, label in GROUPS if include_empty or buckets[code]]


def total(items):
    """Money waiting on a decision, Decimal, ignoring items with no amount."""
    return sum((i.amount for i in items if i.amount is not None), Decimal('0'))


def request_for(user, membership):
    """
    A stand-in request for code outside a request (the daily digest): providers only read
    user, org and membership. Run it inside org_context(membership.organization).
    """
    return SimpleNamespace(user=user, membership=membership, org=membership.organization, project_member=None)


def invalidate(request):
    """Forget the cached list (after an action changed what is waiting)."""
    for attr in ('_needs_me', '_needs_me_portal_ids'):
        if hasattr(request, attr):
            delattr(request, attr)
