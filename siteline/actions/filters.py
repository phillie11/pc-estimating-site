"""
Filters the dashboards link to, so every number on a dashboard opens the list behind it.

    /projects/?view=live&attention=rams       live jobs with RAMS waiting for review
    /bids/?view=open&attention=quotes         open bids with subcontractor quotes still awaited
    /bids/?view=open&due=14                   open bids returning in the next 14 days (not yet submitted)
    /projects/?mine=1                         jobs you manage (bids you lead, on /bids/)

projects.views.project_list calls apply(); the counts on tenancy.views.estimating_home and
delivery_home use the same querysets, so the number and the list always agree.
"""
from datetime import timedelta

from django.db.models import Exists, OuterRef
from django.utils import timezone


def _exists(model_path, **filters):
    def f(qs):
        from django.apps import apps
        model = apps.get_model(model_path)
        return qs.filter(Exists(model.objects.filter(project=OuterRef('pk'), **filters)))
    return f


def _rams(qs):
    from delivery.models import Rams
    return _exists('delivery.Rams', status=Rams.SUBMITTED)(qs)


def _subvars(qs):
    from delivery.models import SubVariation
    return _exists('delivery.SubVariation', status=SubVariation.SUBMITTED)(qs)


def _client_vos(qs):
    from variations.models import Variation
    return _exists('variations.Variation', status=Variation.STATUS_SUBMITTED)(qs)


def _appoint(qs):
    from directory.models import Package
    return _exists('directory.Package', status__in=Package.OPEN_STATUSES)(qs)


def _quotes(qs):
    from directory.models import Package
    return _exists('directory.Package', status=Package.ENQUIRY)(qs)


def _quotes_overdue(qs):
    from directory.models import Package
    return _exists('directory.Package', status=Package.ENQUIRY, quote_due__lt=timezone.localdate())(qs)


def _queries(qs):
    return _exists('tenders.TenderQuery', answered_at__isnull=True)(qs)


ATTENTION = {
    'rams': ('with RAMS waiting for review', _rams),
    'subvars': ('with subcontractor variations to decide', _subvars),
    'client_vos': ('with variations waiting for the client', _client_vos),
    'appoint': ('with trades still out to tender', _appoint),
    'quotes': ('with subcontractor quotes awaited', _quotes),
    'quotes_overdue': ('with subcontractor quotes overdue', _quotes_overdue),
    'queries': ('with unanswered tender queries', _queries),
}


def returns_within(qs, days):
    from projects.models import Project
    return qs.exclude(stage=Project.STAGE_SUBMITTED).filter(tender_due__lte=timezone.now() + timedelta(days=days))


def apply(request, qs, section):
    """Narrow a project list by the dashboard's filters. Returns (queryset, [plain-words description])."""
    said = []
    attention = request.GET.get('attention', '')
    if attention in ATTENTION:
        label, f = ATTENTION[attention]
        qs = f(qs)
        said.append(label)
    due = request.GET.get('due', '')
    if due.isdigit() and due.isascii() and 0 < int(due) <= 365:
        qs = returns_within(qs, int(due))
        said.append(f'returning in the next {int(due)} days')
    if request.GET.get('mine') and request.membership is not None and request.membership.is_internal:
        if section == 'estimating':
            qs = qs.filter(owner=request.user)
            said.append('that you lead')
        else:
            qs = qs.filter(manager=request.user)
            said.append('that you manage')
    return qs, said
