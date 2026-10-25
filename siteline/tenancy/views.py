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
from projects.access import is_internal, visible_projects
from projects.models import Issue, Project, Task
from rfis.models import RFI
from variations.models import Variation

from . import notify
from .forms import MemberRoleForm, OrganizationForm, TeamInviteForm
from .models import ActivityEvent, Membership, TeamInvite, log_activity
from .permissions import admin_required, internal_required


def dashboard(request, org_slug):
    user = request.user
    today = timezone.localdate()
    projects = visible_projects(request)

    my_tasks = Task.objects.filter(assignee=user, project__in=projects).exclude(status=Task.STATUS_DONE) \
        .select_related('project')[:8]
    my_rfis = RFI.objects.filter(assigned_to=user, status=RFI.STATUS_OPEN, project__in=projects) \
        .select_related('project')[:8]
    my_issues = Issue.objects.filter(assignee=user, project__in=projects).exclude(status=Issue.STATUS_CLOSED) \
        .select_related('project')[:8]

    if not is_internal(request):
        my_rfis = RFI.objects.filter(assigned_to=user, status=RFI.STATUS_OPEN, project__in=projects, is_shared=True) \
            .select_related('project')[:8]
        activity = ActivityEvent.objects.filter(project__in=projects, internal_only=False).select_related('actor', 'project')[:15]
        return render(request, 'tenancy/dashboard_external.html', {
            'projects': projects.exclude(stage__in=Project.CLOSED_STAGES),
            'my_tasks': my_tasks, 'my_rfis': my_rfis, 'my_issues': my_issues, 'activity': activity,
        })

    year_ago = today - timedelta(days=365)
    pipeline = projects.filter(stage__in=Project.PIPELINE_STAGES)
    pipeline_value = pipeline.aggregate(s=Sum('value'))['s'] or Decimal('0')
    weighted = sum((p.weighted_value or 0) for p in pipeline.select_related(None).only('value', 'probability'))
    decided = projects.filter(decided_at__gte=year_ago)
    won = decided.filter(stage__in=Project.WON_STAGES).count()
    lost = decided.filter(stage=Project.STAGE_LOST).count()
    win_rate = round(100 * won / (won + lost)) if (won + lost) else None
    won_value = decided.filter(stage__in=Project.WON_STAGES).aggregate(s=Sum('value'))['s'] or Decimal('0')

    open_rfis = RFI.objects.filter(status=RFI.STATUS_OPEN)
    pending_vos = Variation.objects.filter(status=Variation.STATUS_SUBMITTED)

    ctx = {
        'kpi': {
            'live': projects.filter(stage__in=Project.DELIVERY_STAGES).count(),
            'pipeline_value': pipeline_value,
            'pipeline_count': pipeline.count(),
            'weighted': weighted,
            'win_rate': win_rate,
            'won_value': won_value,
            'won': won, 'lost': lost,
            'open_rfis': open_rfis.count(),
            'overdue_rfis': open_rfis.filter(due_date__lt=today).count(),
            'pending_vo_count': pending_vos.count(),
            'pending_vo_value': pending_vos.aggregate(s=Sum('value'))['s'] or Decimal('0'),
        },
        'deadlines': pipeline.filter(tender_due__isnull=False, tender_due__lte=timezone.now() + timedelta(days=14))
                     .exclude(stage=Project.STAGE_SUBMITTED).order_by('tender_due')[:8],
        'insurance': [s for s in Subcontractor.objects.exclude(status=Subcontractor.STATUS_STRUCK_OFF)
                      .filter(Q(public_liability_expiry__lte=today + timedelta(days=30)) |
                              Q(employers_liability_expiry__lte=today + timedelta(days=30)))][:8],
        'my_tasks': my_tasks, 'my_rfis': my_rfis, 'my_issues': my_issues,
        'activity': ActivityEvent.objects.select_related('actor', 'project')[:15],
        'org_empty': not projects.exists(),
    }
    return render(request, 'tenancy/dashboard.html', ctx)


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
        'role_form': MemberRoleForm(), 'seat_limit': org.plan_config['max_staff'],
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
    if not is_internal(request):
        qs = qs.filter(project__in=visible_projects(request), internal_only=False)
    return render(request, 'tenancy/activity.html', {'activity': qs[:200]})
