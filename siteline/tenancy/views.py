from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from directory.models import Subcontractor
from projects.access import is_internal, openable, portal_projects, visible_projects
from projects.models import Issue, Project, Task
from rfis.models import RFI
from variations.models import Variation

from . import notify
from .forms import MemberAreaForm, MemberRoleForm, OrganizationForm, TeamInviteForm
from .models import ActivityEvent, Membership, Notification, TeamInvite, log_activity
from .permissions import admin_required, delivery_required, estimating_required, internal_required


def dashboard(request, org_slug):
    if is_internal(request):
        # Staff land on the dashboard of the side of the business they were last working in
        sections = request.membership.sections
        section = request.session.get('section')
        section = section if section in sections else sections[0]
        return redirect('estimating_home' if section == 'estimating' else 'delivery_home', org_slug=org_slug)

    from directory.models import Package
    user = request.user
    portal = portal_projects(request)
    plate = _my_plate(request, portal)
    plate['my_rfis'] = RFI.objects.filter(ball_in_court_user=user, status=RFI.STATUS_OPEN, project__in=portal,
                                          visibility=RFI.VIS_SHARED) \
        .select_related('project')[:8]
    subs = user.project_memberships.filter(organization=request.org, subcontractor__isnull=False) \
        .values_list('subcontractor_id', flat=True)
    tenders = Package.objects.filter(subcontractor_id__in=subs, status__in=Package.OPEN_STATUSES,
                                     project__members__user=user).distinct().select_related('project', 'trade')
    return render(request, 'tenancy/dashboard_external.html', {
        'tenders': tenders,
        'projects': portal.exclude(stage__in=Project.CLOSED_STAGES),
        'activity': ActivityEvent.objects.filter(project__in=portal, internal_only=False).select_related('actor', 'project')[:15],
        **plate,
    })


def _my_plate(request, projects):
    """
    What is waiting on this person. A snag marked ready for inspection has left the
    assignee's plate: it now waits on staff, so it shows to whoever raised it and to
    the job's project manager as "Ready to inspect".
    """
    user = request.user
    plate = {
        'my_tasks': Task.objects.filter(assignee=user, project__in=projects).exclude(status=Task.STATUS_DONE)
        .select_related('project')[:8],
        'my_rfis': RFI.objects.filter(ball_in_court_user=user, status=RFI.STATUS_OPEN, project__in=projects)
        .select_related('project')[:8],
        'my_issues': Issue.objects.filter(assignee=user, project__in=projects, status=Issue.STATUS_OPEN)
        .select_related('project')[:8],
    }
    if is_internal(request):
        plate['to_inspect'] = Issue.objects.filter(Q(created_by=user) | Q(project__manager=user), project__in=projects,
                                                   status=Issue.STATUS_READY).select_related('project')[:8]
    return plate


@estimating_required
def estimating_home(request, org_slug):
    from directory.models import Package
    from projects.views import use_section
    from tenders.models import TenderQuery
    use_section(request, 'estimating')
    today = timezone.localdate()
    bids = visible_projects(request).filter(stage__in=Project.ESTIMATING_STAGES)
    pipeline = bids.filter(stage__in=Project.PIPELINE_STAGES)
    year_ago = today - timedelta(days=365)
    decided = Project.objects.filter(decided_at__gte=year_ago, archived_at__isnull=True)
    won = decided.filter(stage__in=Project.WON_STAGES).count()
    lost = decided.filter(stage=Project.STAGE_LOST).count()
    awaited = Package.objects.filter(project__in=pipeline, status=Package.ENQUIRY).select_related('project', 'subcontractor', 'trade')
    ctx = {
        'section': 'estimating',
        'kpi': {
            'pipeline_value': pipeline.aggregate(s=Sum('value'))['s'] or Decimal('0'),
            'pipeline_count': pipeline.count(),
            'weighted': sum((p.weighted_value or 0) for p in pipeline.select_related(None).only('value', 'probability')),
            'win_rate': round(100 * won / (won + lost)) if (won + lost) else None, 'won': won, 'lost': lost,
            'won_value': decided.filter(stage__in=Project.WON_STAGES).aggregate(s=Sum('value'))['s'] or Decimal('0'),
            'returns_14': pipeline.exclude(stage=Project.STAGE_SUBMITTED)
            .filter(tender_due__lte=timezone.now() + timedelta(days=14)).count(),
            'quotes_awaited': awaited.count(), 'quotes_overdue': awaited.filter(quote_due__lt=today).count(),
            'queries': TenderQuery.objects.filter(project__in=pipeline, answered_at__isnull=True).count(),
        },
        'deadlines': pipeline.filter(tender_due__isnull=False, tender_due__lte=timezone.now() + timedelta(days=14))
                     .exclude(stage=Project.STAGE_SUBMITTED).order_by('tender_due')[:8],
        'awaited': awaited.order_by('quote_due')[:10],
        'my_bids': pipeline.filter(owner=request.user).order_by('tender_due')[:8],
        'activity': ActivityEvent.objects.filter(project__in=bids).select_related('actor', 'project')[:12],
        'org_empty': not Project.objects.exists(),
    }
    return render(request, 'tenancy/estimating_home.html', ctx)


@delivery_required
def delivery_home(request, org_slug):
    from delivery.models import Rams, SubVariation
    from directory.models import Package
    from projects.views import use_section
    use_section(request, 'delivery')
    today = timezone.localdate()
    jobs = visible_projects(request).exclude(stage__in=Project.ESTIMATING_STAGES)
    live = jobs.filter(stage__in=Project.DELIVERY_STAGES)
    rams = Rams.objects.filter(project__in=live, status=Rams.SUBMITTED).select_related('project', 'subcontractor')
    subvars = SubVariation.objects.filter(project__in=live, status=SubVariation.SUBMITTED).select_related('project', 'subcontractor')
    open_rfis = RFI.objects.filter(project__in=live, status=RFI.STATUS_OPEN)
    client_vos = Variation.objects.filter(project__in=live, status=Variation.STATUS_SUBMITTED)
    ctx = {
        'section': 'delivery',
        'kpi': {
            'live': live.count(), 'mine': live.filter(manager=request.user).count(),
            'rams': rams.count(), 'subvars': subvars.count(),
            'subvars_value': subvars.aggregate(s=Sum('value'))['s'] or Decimal('0'),
            'client_vos': client_vos.count(),
            'client_vos_value': client_vos.aggregate(s=Sum('value'))['s'] or Decimal('0'),
            'open_rfis': open_rfis.count(), 'overdue_rfis': open_rfis.filter(due_date__lt=today).count(),
            'to_appoint': Package.objects.filter(project__in=live, status__in=Package.OPEN_STATUSES)
            .values('project', 'trade').distinct().count(),
        },
        'unassigned': [p for p in live if p.needs_handover],
        'my_jobs': live.filter(manager=request.user).order_by('completion_date'),
        'rams': rams.order_by('submitted_at')[:8],
        'subvars': subvars.order_by('created_at')[:8],
        'insurance': [s for s in Subcontractor.objects.filter(packages__project__in=live, packages__status=Package.APPOINTED,
                                                              archived_at__isnull=True)
                      .distinct().filter(Q(public_liability_expiry__lte=today + timedelta(days=30)) |
                                         Q(employers_liability_expiry__lte=today + timedelta(days=30)))][:8],
        'activity': ActivityEvent.objects.filter(project__in=jobs).select_related('actor', 'project')[:12],
        **_my_plate(request, live),
    }
    return render(request, 'tenancy/delivery_home.html', ctx)


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------

@internal_required
def team(request, org_slug):
    org = request.org
    staff = org.memberships.filter(is_active=True).exclude(role=Membership.ROLE_EXTERNAL).select_related('user')
    externals = org.memberships.filter(is_active=True, role=Membership.ROLE_EXTERNAL).select_related('user') \
        .annotate(n_projects=Count('user__project_memberships', filter=Q(user__project_memberships__organization=org)))
    invites = org.team_invites.filter(status=TeamInvite.STATUS_PENDING)
    form = TeamInviteForm(org=org)
    if request.method == 'POST':
        if not request.membership.is_admin:
            raise Http404
        form = TeamInviteForm(request.POST, org=org)
        if not org.can_add_staff():
            messages.error(request, f'Your {org.plan_config["name"]} plan allows {org.plan_config["max_staff"]} staff seats. '
                                    'Upgrade to invite more. External collaborators are always free.')
        elif form.is_valid():
            inv = form.save(commit=False)
            inv.organization, inv.invited_by = org, request.user
            inv.save()
            notify.send(inv.email, f'{request.user.full_name} invited you to {org.name}', 'team_invite', {
                'inviter': request.user, 'link': notify.absolute(reverse('accept_team_invite', args=[inv.token])),
            }, org=org)
            log_activity(request, 'invited', inv.email)
            messages.success(request, f'Invite sent to {inv.email}.')
            return redirect('team', org_slug=org.slug)
    return render(request, 'tenancy/team.html', {
        'staff': staff, 'externals': externals, 'invites': invites, 'form': form,
        'role_form': MemberRoleForm(), 'area_choices': Membership.AREA_CHOICES, 'seat_limit': org.plan_config['max_staff'],
        'seats_used': org.staff_count() + invites.count(),
    })


@require_POST
@admin_required
def member_update(request, org_slug, pk):
    m = get_object_or_404(Membership, organization=request.org, pk=pk)
    action = request.POST.get('action')
    owners = request.org.memberships.filter(role=Membership.ROLE_OWNER, is_active=True)
    if m.role == Membership.ROLE_OWNER and owners.count() == 1 and action in ('remove', 'role'):
        if action == 'remove' or request.POST.get('role') != Membership.ROLE_OWNER:
            messages.error(request, 'A workspace needs at least one owner.')
            return redirect('team', org_slug=org_slug)
    if action == 'remove':
        if m.role == Membership.ROLE_OWNER and not request.membership.is_owner:
            messages.error(request, 'Only an owner can remove an owner.')
        elif m.user == request.user:
            messages.error(request, 'You cannot remove yourself.')
        else:
            m.is_active = False
            m.save(update_fields=['is_active'])
            log_activity(request, 'removed', m.user.full_name)
            messages.success(request, f'{m.user.full_name} removed.')
    elif action == 'role':
        form = MemberRoleForm(request.POST)
        if form.is_valid():
            new_role = form.cleaned_data['role']
            if (new_role == Membership.ROLE_OWNER or m.role == Membership.ROLE_OWNER) and not request.membership.is_owner:
                messages.error(request, 'Only an owner can grant or remove ownership.')
            elif m.role == Membership.ROLE_EXTERNAL and not request.org.can_add_staff():
                messages.error(request, 'No staff seats left on your plan.')
            else:
                m.role = new_role
                m.save(update_fields=['role'])
                messages.success(request, f'{m.user.full_name} is now {m.get_role_display()}.')
    elif action == 'area':
        form = MemberAreaForm(request.POST)
        if form.is_valid():
            m.area = form.cleaned_data['area']
            m.save(update_fields=['area'])
            messages.success(request, f'{m.user.full_name} now works in {m.get_area_display().lower()}.')
    return redirect('team', org_slug=org_slug)


@require_POST
@admin_required
def invite_revoke(request, org_slug, pk):
    inv = get_object_or_404(TeamInvite, organization=request.org, pk=pk)
    inv.status = TeamInvite.STATUS_REVOKED
    inv.save(update_fields=['status'])
    messages.success(request, 'Invite revoked.')
    return redirect('team', org_slug=org_slug)


# ---------------------------------------------------------------------------
# Settings, logo, lock page
# ---------------------------------------------------------------------------

@admin_required
def org_settings(request, org_slug):
    form = OrganizationForm(request.POST or None, request.FILES or None, instance=request.org)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Company settings saved.')
        return redirect('org_settings', org_slug=org_slug)
    return render(request, 'tenancy/settings.html', {'form': form})


def org_logo(request, org_slug):
    if not request.org.logo:
        raise Http404
    resp = FileResponse(request.org.logo.open('rb'))
    resp['Cache-Control'] = 'private, max-age=3600'
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp


def org_locked(request, org_slug):
    if request.org.has_access:
        return redirect('dashboard', org_slug=org_slug)
    return render(request, 'tenancy/locked.html', {'state': request.org.access_state()}, status=402)


def activity(request, org_slug):
    qs = ActivityEvent.objects.select_related('actor', 'project')
    if is_internal(request):
        qs = qs.filter(openable(request))
    else:
        qs = qs.filter(project__in=portal_projects(request), internal_only=False)
    return render(request, 'tenancy/activity.html', {'activity': qs[:200]})


# ---------------------------------------------------------------------------
# Notifications (the bell)
# ---------------------------------------------------------------------------

def notifications(request, org_slug):
    items = Notification.objects.filter(openable(request), user=request.user).select_related('actor', 'project')
    if request.method == 'POST' and request.POST.get('action') == 'read_all':
        items.filter(read_at__isnull=True).update(read_at=timezone.now())
        return redirect('notifications', org_slug=org_slug)
    show = request.GET.get('show', 'all')
    if show == 'unread':
        items = items.filter(read_at__isnull=True)
    return render(request, 'tenancy/notifications.html', {'items': items[:100], 'show': show})


def notification_open(request, org_slug, pk):
    n = get_object_or_404(Notification.objects.filter(openable(request)), user=request.user, pk=pk)
    if n.read_at is None:
        n.read_at = timezone.now()
        n.save(update_fields=['read_at'])
    # Stored URLs are always site paths; refuse anything else rather than redirect off-site
    if not n.url.startswith('/') or n.url.startswith('//'):
        return redirect('notifications', org_slug=org_slug)
    return redirect(n.url)
