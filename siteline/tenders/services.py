"""
The rules of a portal tender: who gets access, who is told, and what happens
when a package is decided.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from directory.models import Package
from projects.models import ProjectInvite, ProjectMember
from tenancy import notify
from tenancy.models import log_activity


def tender_url(project):
    return reverse('tender', args=[project.organization.slug, project.pk])


def sub_members(project, subcontractor_ids):
    """Portal users on this project who belong to the given subcontractors."""
    return [m.user for m in ProjectMember.objects.filter(project=project, subcontractor_id__in=subcontractor_ids)
            .select_related('user')]


def open_bidders(project):
    """Everyone currently pricing something on this project."""
    ids = Package.objects.filter(project=project, status__in=Package.OPEN_STATUSES).values_list('subcontractor_id', flat=True)
    return sub_members(project, set(ids))


def watchers(project, *extra):
    """Contractor staff who should hear about tender activity on this project."""
    users = [project.owner, *extra]
    users += [m.user for m in ProjectMember.objects.filter(
        project=project, role__in=[r for r, _ in ProjectMember.INTERNAL_ROLE_CHOICES]).select_related('user')]
    users += [p.invited_by for p in Package.objects.filter(project=project).select_related('invited_by')]
    return [u for u in users if u is not None]


def invite(request, project, subcontractor, trade, scope, quote_due):
    """
    Invite one subcontractor to price one trade. Returns the package, or None if
    they are already invited for it. Existing portal users are added and told;
    new people get a one-off link to set up their free account.
    """
    if Package.objects.filter(project=project, subcontractor=subcontractor, trade=trade).exists():
        return None
    pkg = Package.objects.create(project=project, subcontractor=subcontractor, trade=trade, scope=scope,
                                 quote_due=quote_due, status=Package.ENQUIRY, invited_by=request.user,
                                 sent_at=timezone.now())
    email = subcontractor.email.lower()
    title = f'Invitation to tender: {trade or "package"} on {project.name}'
    member = ProjectMember.objects.filter(project=project, user__email__iexact=email).select_related('user').first()
    if member is None:
        user = get_user_model().objects.filter(email__iexact=email, memberships__organization=project.organization,
                                                memberships__is_active=True).first()
        if user is not None:
            member = ProjectMember.objects.create(project=project, user=user, role=ProjectMember.ROLE_BIDDER,
                                                  company_name=subcontractor.company_name, subcontractor=subcontractor,
                                                  added_by=request.user)
    if member is not None:
        if member.subcontractor_id is None:
            member.subcontractor = subcontractor
            member.save(update_fields=['subcontractor'])
        notify.alert([member.user], title, tender_url(project), project=project, actor=request.user, org=request.org)
    else:
        inv = ProjectInvite.objects.filter(project=project, email=email, status=ProjectInvite.STATUS_PENDING).first() \
            or ProjectInvite.objects.create(project=project, email=email, role=ProjectMember.ROLE_BIDDER,
                                            company_name=subcontractor.company_name, subcontractor=subcontractor,
                                            invited_by=request.user)
        notify.send(email, title, 'tender_invite', {
            'inviter': request.user, 'project': project, 'package': pkg,
            'link': notify.absolute(reverse('accept_project_invite', args=[inv.token])),
        }, org=request.org)
    log_activity(request, f'invited {subcontractor.company_name} to price', str(trade or 'a package'), project=project)
    return pkg


def revoke_if_finished(project, subcontractor):
    """A bidder whose packages here are all declined or unsuccessful loses access to the tender."""
    if Package.objects.filter(project=project, subcontractor=subcontractor) \
            .exclude(status__in=Package.CLOSED_STATUSES).exists():
        return
    ProjectMember.objects.filter(project=project, subcontractor=subcontractor, role=ProjectMember.ROLE_BIDDER).delete()
    ProjectInvite.objects.filter(project=project, subcontractor=subcontractor, role=ProjectMember.ROLE_BIDDER,
                                 status=ProjectInvite.STATUS_PENDING).update(status=ProjectInvite.STATUS_REVOKED)


def mark_unsuccessful(request, pkg, tell=True):
    pkg.status, pkg.decided_at = Package.UNSUCCESSFUL, timezone.now()
    pkg.save(update_fields=['status', 'decided_at'])
    if tell:
        notify.alert(sub_members(pkg.project, [pkg.subcontractor_id]),
                     f'Tender outcome: {pkg.trade or "package"} on {pkg.project.name}. Thank you for pricing; '
                     f'on this occasion your quote was not successful.',
                     reverse('dashboard', args=[pkg.project.organization.slug]), project=pkg.project,
                     actor=request.user, org=request.org)
    revoke_if_finished(pkg.project, pkg.subcontractor)


@transaction.atomic
def appoint(request, pkg):
    """Appoint one bidder for a trade: they join the project; the rest of that trade are told and lose access."""
    project = pkg.project
    pkg.status, pkg.decided_at = Package.APPOINTED, timezone.now()
    pkg.save(update_fields=['status', 'decided_at'])
    ProjectMember.objects.filter(project=project, subcontractor=pkg.subcontractor, role=ProjectMember.ROLE_BIDDER) \
        .update(role=ProjectMember.ROLE_SUBCONTRACTOR)
    ProjectInvite.objects.filter(project=project, subcontractor=pkg.subcontractor, role=ProjectMember.ROLE_BIDDER,
                                 status=ProjectInvite.STATUS_PENDING).update(role=ProjectMember.ROLE_SUBCONTRACTOR)
    notify.alert(sub_members(project, [pkg.subcontractor_id]),
                 f'You have been appointed: {pkg.trade or "package"} on {project.name}',
                 project.get_absolute_url(), project=project, actor=request.user, org=request.org)
    others = Package.objects.filter(project=project, trade=pkg.trade, status__in=Package.OPEN_STATUSES).exclude(pk=pkg.pk)
    for other in others.select_related('subcontractor', 'trade', 'project'):
        mark_unsuccessful(request, other)
    log_activity(request, f'appointed {pkg.subcontractor}', str(pkg.trade or ''), project=project)


def close_all(request, project):
    """The main tender is lost or declined: close every open package and tell the bidders."""
    for pkg in Package.objects.filter(project=project, status__in=Package.OPEN_STATUSES) \
            .select_related('subcontractor', 'trade', 'project'):
        pkg.status, pkg.decided_at = Package.UNSUCCESSFUL, timezone.now()
        pkg.save(update_fields=['status', 'decided_at'])
        notify.alert(sub_members(project, [pkg.subcontractor_id]),
                     f'Tender closed: {project.name} will not be going ahead with us. Thank you for pricing.',
                     reverse('dashboard', args=[project.organization.slug]), project=project, actor=request.user,
                     org=request.org)
        revoke_if_finished(project, pkg.subcontractor)
