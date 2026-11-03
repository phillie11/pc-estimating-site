"""
The getting-started checklist on the dashboards for a new company's owners and admins.

Each step ticks itself from the company's own data, so there is nothing to keep in step: a logo or
colour, a second person, the cost code library, a first project, a subcontractor invited to a project
or a tender. It shows until every step is done or someone dismisses it (help.OnboardingState).
"""
from dataclasses import dataclass

from django.db.models import Count, Exists, OuterRef, Q
from django.urls import reverse

from tenancy.models import Membership, Organization, TeamInvite


@dataclass
class Step:
    key: str
    label: str
    detail: str
    url: str
    action: str
    done: bool


def _flags(org):
    """Every step's done-flag in one query. Each subquery is pinned to this organisation by OuterRef('pk')."""
    from costs.models import CostSettings
    from projects.models import Project, ProjectInvite, ProjectMember

    from .models import OnboardingState
    org_ref = OuterRef('pk')
    sub_roles = (ProjectMember.ROLE_SUBCONTRACTOR, ProjectMember.ROLE_BIDDER)
    return Organization.objects.filter(pk=org.pk).annotate(
        staff=Count('memberships', filter=Q(memberships__is_active=True,
                                            memberships__role__in=Membership.INTERNAL_ROLES)),
        invited=Exists(TeamInvite.objects.filter(organization=org_ref, status=TeamInvite.STATUS_PENDING)),
        codes=Exists(CostSettings.all_objects.filter(organization=org_ref)),
        project=Exists(Project.all_objects.filter(organization=org_ref)),
        sub=Exists(ProjectMember.all_objects.filter(organization=org_ref, role__in=sub_roles))
        | Exists(ProjectInvite.all_objects.filter(organization=org_ref, role__in=sub_roles)),
        dismissed=Exists(OnboardingState.all_objects.filter(organization=org_ref, dismissed_at__isnull=False)),
    ).values('staff', 'invited', 'codes', 'project', 'sub', 'dismissed').first() or {}


def steps(org, flags=None):
    flags = flags if flags is not None else _flags(org)
    slug = org.slug
    branded = bool(org.logo) or org.brand_colour.upper() != Organization._meta.get_field('brand_colour').default
    if flags.get('project') and not flags.get('sub'):
        from projects.models import Project  # a request is running, so this is this company's latest
        latest = Project.objects.filter(archived_at__isnull=True).order_by('-pk').only('pk').first()
        sub_url = reverse('project_team', args=[slug, latest.pk]) if latest else reverse('sub_list', args=[slug])
    else:
        sub_url = reverse('sub_list', args=[slug])
    return [
        Step('brand', 'Add your logo and colour', 'Your clients and subcontractors see them in their portal and on '
             'every PDF.', reverse('org_settings', args=[slug]), 'Open settings', branded),
        Step('team', 'Invite your team', 'Add the people who price work and run jobs, and say which they do.',
             reverse('team', args=[slug]), 'Invite people', flags.get('staff', 0) > 1 or bool(flags.get('invited'))),
        Step('codes', 'Set up your cost codes', 'Start from the standard UK fit-out list and change what you need.',
             reverse('cost_settings', args=[slug]), 'Set up cost codes', bool(flags.get('codes'))),
        Step('project', 'Add your first project', 'A bid you are pricing, or a job already on site.',
             reverse('project_create', args=[slug]), 'Add a project', bool(flags.get('project'))),
        Step('sub', 'Invite a subcontractor', 'Invite them to a project or to price a tender. Their login is free.',
             sub_url, 'Invite a subcontractor', bool(flags.get('sub'))),
    ]


def checklist(request):
    """The checklist for this person's dashboard, or None when it shouldn't show."""
    membership = getattr(request, 'membership', None)
    if membership is None or not membership.is_admin:
        return None
    org = request.org
    flags = _flags(org)
    if flags.get('dismissed'):
        return None
    items = steps(org, flags)
    done = sum(1 for s in items if s.done)
    if done == len(items):
        return None
    return {'steps': items, 'done': done, 'total': len(items), 'next': next(s for s in items if not s.done)}


def dismiss(request):
    from django.utils import timezone

    from .models import OnboardingState
    state = OnboardingState.objects.filter(organization=request.org).first() or OnboardingState(organization=request.org)
    state.dismissed_at, state.dismissed_by = timezone.now(), request.user
    state.save()

