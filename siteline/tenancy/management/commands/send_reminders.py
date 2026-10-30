"""
Daily digest. Schedule on PythonAnywhere (Tasks tab), e.g. 07:00 daily:
    cd ~/siteline && source .venv/bin/activate && python manage.py send_reminders

What is waiting on each person comes from the "Needs me" registry (actions), the same list
as their Needs me page, so the email and the page always agree. Staff also get tender
returns in the next three days, subcontractor insurance expiring and the trial reminder.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

import actions
from directory.models import Subcontractor
from projects.models import Project
from tenancy import notify
from tenancy.context import org_context
from tenancy.models import Membership, Organization

# Groups the digest lists item by item; anything due further off is only counted
LISTED_GROUPS = ('overdue', 'today', 'week')


def waiting_for(user, membership):
    """(sections, later_count): [(label, [item...])] for the email, each item with an absolute link."""
    items = actions.for_user(actions.request_for(user, membership))
    for item in items:
        item.link = notify.absolute(item.url)
    sections, later = [], 0
    for code, label, rows in actions.grouped(items):
        if code in LISTED_GROUPS:
            sections.append((label, rows))
        else:
            undated = [i for i in rows if i.due is None]
            later += len(rows) - len(undated)
            if undated:
                sections.append(('Waiting on you, no date set', undated))
    return sections, later


class Command(BaseCommand):
    help = ('Email each person a digest of what is waiting on them (their Needs me list), plus, for staff, '
            'tender deadlines, insurance expiry and trial status.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, dry_run=False, **opts):
        today = timezone.localdate()
        sent = 0
        for org in Organization.objects.filter(is_suspended=False):
            if not org.has_access:
                continue
            with org_context(org):
                tenders = list(Project.objects.filter(stage__in=[Project.STAGE_LEAD, Project.STAGE_TENDERING],
                                                      archived_at__isnull=True,
                                                      tender_due__gte=timezone.now(),
                                                      tender_due__lte=timezone.now() + timedelta(days=3)))
                soon = today + timedelta(days=14)
                insurance = list(Subcontractor.objects.exclude(status=Subcontractor.STATUS_STRUCK_OFF).filter(
                    Q(public_liability_expiry__range=(today, soon)) | Q(employers_liability_expiry__range=(today, soon)),
                    archived_at__isnull=True))
                trial_days = org.trial_days_left if org.is_on_internal_trial and org.trial_days_left in (7, 3, 1) else None
                for m in org.memberships.filter(is_active=True).select_related('user', 'organization'):
                    user = m.user
                    if not user.is_active:
                        continue
                    waiting, later = waiting_for(user, m)
                    ctx = {'waiting': waiting, 'later': later, 'needs_me_link': notify.absolute(reverse('needs_me', args=[org.slug]))}
                    if m.is_internal:
                        # Estimators see tender returns; everyone else only if they can see bids
                        ctx.update({
                            'tenders': tenders if m.can_estimate else [],
                            'insurance': insurance if m.is_admin or m.role == Membership.ROLE_MEMBER else [],
                            'trial_days': trial_days if m.is_admin else None,
                        })
                    if ctx['waiting'] or ctx.get('tenders') or ctx.get('insurance') or ctx.get('trial_days') is not None:
                        sent += self._send(user, org, ctx, dry_run)
        self.stdout.write(self.style.SUCCESS(f'{sent} digest(s) {"would be " if dry_run else ""}sent'))

    def _send(self, user, org, ctx, dry_run):
        if dry_run:
            self.stdout.write(f'  {org.slug}: {user.email}')
            return 1
        notify.send(user.email, 'Your daily round-up', 'digest', {'user': user, **ctx}, org=org)
        return 1
