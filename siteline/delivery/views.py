from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from directory.models import Package
from projects.access import get_project, is_internal
from projects.files import serve
from projects.forms import CommentForm
from projects.models import ProjectMember
from projects.views import comments_for, handle_comment, project_context
from tenancy import notify
from tenancy.models import log_activity
from variations.models import Variation

from . import access
from .forms import RamsForm, RamsReviewForm, ResubmitForm, SubVariationDecisionForm, SubVariationForm
from .models import Rams, SubVariation


def site_team(project):
    """The contractor's people on a live job: the project manager and staff on the project team."""
    users = [project.manager] + [m.user for m in ProjectMember.objects.filter(
        project=project, role__in=[r for r, _ in ProjectMember.INTERNAL_ROLE_CHOICES]).select_related('user')]
    users = [u for u in users if u]
    return users or [project.owner]


def sub_users(project, subcontractor_id):
    return [m.user for m in ProjectMember.objects.filter(project=project, subcontractor_id=subcontractor_id,
                                                         role=ProjectMember.ROLE_SUBCONTRACTOR).select_related('user')]


def _appointed_package(project, subcontractor_id):
    return Package.objects.filter(project=project, subcontractor_id=subcontractor_id, status=Package.APPOINTED).first()


def _scoped(qs, sub):
    return qs if sub is None else qs.filter(subcontractor_id=sub)


# ---------------------------------------------------------------------------
# RAMS
# ---------------------------------------------------------------------------

def rams_list(request, org_slug, pk):
    project = get_project(request, pk)
    sub = access.scope(request)
    form = RamsForm() if sub else None
    if request.method == 'POST':
        if sub is None:
            raise PermissionDenied
        form = RamsForm(request.POST, request.FILES)
        if form.is_valid():
            r = form.save(commit=False)
            r.project, r.subcontractor_id, r.submitted_by = project, sub, request.user
            r.package = _appointed_package(project, sub)
            r.original_name = r.file.name[:200]
            r.save()
            notify.alert(site_team(project), f'RAMS to review: {r.title} ({r.subcontractor})', r.get_absolute_url(),
                         project=project, actor=request.user, org=request.org)
            log_activity(request, 'submitted RAMS', r.title, project=project, url=r.get_absolute_url())
            messages.success(request, f'{r.reference} submitted for review.')
            return redirect(r)
    status = request.GET.get('status', 'current')
    rams = _scoped(Rams.objects.filter(project=project), sub).select_related('subcontractor', 'reviewed_by')
    if status == 'current':
        rams = rams.exclude(status=Rams.SUPERSEDED)
    elif status in dict(Rams.STATUS_CHOICES):
        rams = rams.filter(status=status)
    return render(request, 'delivery/rams_list.html', project_context(
        request, project, 'rams', rams=rams, form=form, status=status, status_choices=Rams.STATUS_CHOICES))


def rams_detail(request, org_slug, pk, rams_id):
    project = get_project(request, pk)
    sub = access.scope(request)
    r = get_object_or_404(_scoped(Rams.objects.filter(project=project), sub).select_related('subcontractor'), pk=rams_id)
    review_form, resubmit_form = RamsReviewForm(), ResubmitForm()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'review' and is_internal(request) and r.status == Rams.SUBMITTED:
            review_form = RamsReviewForm(request.POST)
            if review_form.is_valid():
                r.status = review_form.cleaned_data['decision']
                r.review_comments = review_form.cleaned_data['comments']
                r.reviewed_by, r.reviewed_at = request.user, timezone.now()
                r.save()
                notify.alert(sub_users(project, r.subcontractor_id), f'{r.reference} {r.get_status_display().lower()}',
                             r.get_absolute_url(), project=project, actor=request.user, org=request.org)
                log_activity(request, f'reviewed RAMS ({r.get_status_display().lower()})', r.title, project=project,
                             url=r.get_absolute_url())
                messages.success(request, f'{r.reference}: {r.get_status_display()}.')
                return redirect(r)
        elif action == 'resubmit' and sub and r.status == Rams.REJECTED:
            resubmit_form = ResubmitForm(request.POST, request.FILES)
            if resubmit_form.is_valid():
                with transaction.atomic():
                    f = resubmit_form.cleaned_data['file']
                    new = Rams.objects.create(
                        project=project, subcontractor_id=sub, package=r.package, number=r.number,
                        revision=r.revision + 1, title=r.title, scope=resubmit_form.cleaned_data['note'] or r.scope,
                        file=f, original_name=f.name[:200], submitted_by=request.user)
                    r.status = Rams.SUPERSEDED
                    r.save(update_fields=['status'])
                notify.alert(site_team(project), f'RAMS resubmitted: {new.reference} ({new.subcontractor})',
                             new.get_absolute_url(), project=project, actor=request.user, org=request.org)
                messages.success(request, f'{new.reference} submitted.')
                return redirect(new)
        else:
            raise PermissionDenied
    history = Rams.objects.filter(project=project, number=r.number).order_by('-revision')
    return render(request, 'delivery/rams_detail.html', project_context(
        request, project, 'rams', r=r, history=history, review_form=review_form, resubmit_form=resubmit_form,
        can_review=is_internal(request) and r.status == Rams.SUBMITTED,
        can_resubmit=sub is not None and r.status == Rams.REJECTED))


def rams_file(request, org_slug, pk, rams_id):
    project = get_project(request, pk)
    r = get_object_or_404(_scoped(Rams.objects.filter(project=project), access.scope(request)), pk=rams_id)
    return serve(request, r.file, r.original_name or None, project=project, label=r.reference)


# ---------------------------------------------------------------------------
# Subcontractor variations
# ---------------------------------------------------------------------------

def subvariation_list(request, org_slug, pk):
    project = get_project(request, pk)
    sub = access.scope(request)
    form = SubVariationForm() if sub else None
    if request.method == 'POST':
        if sub is None:
            raise PermissionDenied
        form = SubVariationForm(request.POST, request.FILES)
        if form.is_valid():
            v = form.save(commit=False)
            v.project, v.subcontractor_id, v.submitted_by = project, sub, request.user
            v.package = _appointed_package(project, sub)
            v.save()
            notify.alert(site_team(project), f'Sub variation {v.reference} from {v.subcontractor}: {v.title}',
                         v.get_absolute_url(), project=project, actor=request.user, org=request.org)
            log_activity(request, 'submitted variation', f'{v.reference} {v.title}', project=project, url=v.get_absolute_url())
            messages.success(request, f'{v.reference} submitted.')
            return redirect(v)
    items = _scoped(SubVariation.objects.filter(project=project), sub).select_related('subcontractor', 'client_variation')
    status = request.GET.get('status', '')
    if status in dict(SubVariation.STATUS_CHOICES):
        items = items.filter(status=status)
    totals = _scoped(SubVariation.objects.filter(project=project), sub).aggregate(
        claimed=Sum('value', filter=~Q(status=SubVariation.WITHDRAWN)),
        approved=Sum('approved_value', filter=Q(status=SubVariation.APPROVED)),
        pending=Sum('value', filter=Q(status=SubVariation.SUBMITTED)))
    return render(request, 'delivery/subvariation_list.html', project_context(
        request, project, 'subvariations', items=items, form=form, status=status, totals=totals,
        status_choices=SubVariation.STATUS_CHOICES))


def subvariation_detail(request, org_slug, pk, sv_id):
    project = get_project(request, pk)
    sub = access.scope(request)
    v = get_object_or_404(_scoped(SubVariation.objects.filter(project=project), sub).select_related('subcontractor'),
                          pk=sv_id)
    decision_form = SubVariationDecisionForm(initial={'approved_value': v.value})
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'comment':
            handle_comment(request, v, project, v.reference)
            return redirect(v)
        if action == 'decide':
            if not (is_internal(request) and v.status == SubVariation.SUBMITTED):
                raise PermissionDenied
            decision_form = SubVariationDecisionForm(request.POST)
            if decision_form.is_valid():
                d = decision_form.cleaned_data
                v.status = d['decision']
                v.approved_value = (d['approved_value'] if d['approved_value'] is not None else v.value) \
                    if v.status == SubVariation.APPROVED else None
                v.decision_notes, v.decided_by, v.decided_at = d['decision_notes'], request.user, timezone.now()
                with transaction.atomic():
                    if v.status == SubVariation.APPROVED and d['raise_client_variation'] and not v.client_variation_id:
                        vo = Variation(
                            # The client reads the description once it's submitted: no subcontractor or SV ref
                            # there. Where it came from stays internal (source_note, sub_variations FK, links).
                            project=project, title=v.title, description=v.description,
                            source_note=f'From {v.reference}, {v.subcontractor}'[:300],
                            cause='CLIENT_INSTRUCTION' if v.reason == 'INSTRUCTION' else 'DESIGN_CHANGE' if v.reason == 'DESIGN'
                            else 'SITE_CONDITION' if v.reason == 'SITE' else 'OTHER',
                            value=v.approved_value, time_impact_days=v.time_impact_days)
                        vo.save(actor=request.user)  # numbered, indexed and audited under the decider's name
                        if v.change_event_id:
                            # The draft VO came from the same change event as the sub variation
                            from changes.links import set_source
                            set_source(vo, v.change_event, 'changes.changeevent', actor=request.user)
                        v.client_variation = vo
                    v.save()
                verb = 'approved' if v.status == SubVariation.APPROVED else 'rejected'
                notify.alert(sub_users(project, v.subcontractor_id), f'{v.reference} {verb}: {v.title}', v.get_absolute_url(),
                             project=project, actor=request.user, org=request.org)
                log_activity(request, f'{verb} sub variation', f'{v.reference} {v.title}', project=project,
                             url=v.get_absolute_url())
                messages.success(request, f'{v.reference} {verb}.' + (
                    f' Draft {v.client_variation.reference} created for the client.' if v.client_variation_id and
                    d['raise_client_variation'] else ''))
                return redirect(v)
        elif action == 'withdraw' and sub and v.status == SubVariation.SUBMITTED:
            v.status = SubVariation.WITHDRAWN
            v.save(update_fields=['status'])
            notify.alert(site_team(project), f'{v.reference} withdrawn by {v.subcontractor}', v.get_absolute_url(),
                         project=project, actor=request.user, org=request.org)
            return redirect(v)
        else:
            raise PermissionDenied
    return render(request, 'delivery/subvariation_detail.html', project_context(
        request, project, 'subvariations', v=v, decision_form=decision_form,
        can_decide=is_internal(request) and v.status == SubVariation.SUBMITTED,
        comments=comments_for(request, v), comment_form=CommentForm(external=not is_internal(request))))


def subvariation_file(request, org_slug, pk, sv_id):
    project = get_project(request, pk)
    v = get_object_or_404(_scoped(SubVariation.objects.filter(project=project), access.scope(request)), pk=sv_id)
    return serve(request, v.file, project=project, label=f'{v.reference} supporting document')


def subcontract_summary(project, subcontractor_id=None):
    """Appointed value plus approved variations, per subcontractor. Three queries however many packages."""
    pkgs = list(Package.objects.filter(project=project, status=Package.APPOINTED).select_related('subcontractor', 'trade'))
    if subcontractor_id:
        pkgs = [p for p in pkgs if p.subcontractor_id == subcontractor_id]
    subs = {p.subcontractor_id for p in pkgs}
    money = {}  # (subcontractor, status) -> sum
    for row in SubVariation.objects.filter(project=project, subcontractor_id__in=subs,
                                           status__in=(SubVariation.APPROVED, SubVariation.SUBMITTED)) \
            .values('subcontractor_id', 'status').annotate(approved=Sum('approved_value'), quoted=Sum('value')):
        money[(row['subcontractor_id'], row['status'])] = row['approved' if row['status'] == SubVariation.APPROVED
                                                              else 'quoted']
    rams = {}  # subcontractor -> {status: count}
    for sub_id, status in Rams.objects.filter(project=project, subcontractor_id__in=subs) \
            .exclude(status=Rams.SUPERSEDED).values_list('subcontractor_id', 'status'):
        rams.setdefault(sub_id, {}).setdefault(status, 0)
        rams[sub_id][status] += 1
    rows = []
    for p in pkgs:
        approved = money.get((p.subcontractor_id, SubVariation.APPROVED)) or Decimal('0')
        pending = money.get((p.subcontractor_id, SubVariation.SUBMITTED)) or Decimal('0')
        counts = rams.get(p.subcontractor_id, {})
        rows.append({'package': p, 'order': p.quote_value or Decimal('0'), 'approved': approved, 'pending': pending,
                     'total': (p.quote_value or Decimal('0')) + approved,
                     'rams_ok': sum(counts.get(s, 0) for s in Rams.ACCEPTED),
                     'rams_waiting': counts.get(Rams.SUBMITTED, 0),
                     'rams_rejected': counts.get(Rams.REJECTED, 0)})
    return rows
