"""
Round-up email. Schedule on PythonAnywhere (Tasks tab), e.g. 07:00 daily:
    cd ~/siteline && source .venv/bin/activate && python manage.py send_reminders

What is waiting on each person comes from the "Needs me" registry (actions), the same list
as their Needs me page. Staff also get tender returns in the next three days, subcontractor
insurance expiring and the trial reminder.

The daily one is sent only when something is urgent: an item overdue or due within 3 days, a
tender return within 3 days, insurance lapsing within 3 days, or the trial ending within 3 days.
Undated items and things further off are listed when it goes, never a reason to send it.
The weekly one is a full summary, sent whenever anything is waiting (actions/digest.py). Each person chooses daily (default), weekly on Mondays, or off, on
their profile or from the signed unsubscribe link in every email.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

import actions
from actions import digest
from actions.models import DigestPreference
from directory.models import Subcontractor
from projects.models import Project
from tenancy import notify
from tenancy.context import org_context
from tenancy.models import Membership, Organization


def waiting_for(user, membership, today=None, everything=False):
    """(urgent, sections, more) for this person, each item with an absolute link."""
    items = actions.for_user(actions.request_for(user, membership))
    for item in items:
        item.link = notify.absolute(item.url)
    return digest.sections_for(items, today, cap=None if everything else digest.EXTRA_CAP)


class Command(BaseCommand):
    help = ('Email each person a round-up of what is waiting on them (their Needs me list), plus, for staff, '
            'tender deadlines, insurance expiry and trial status, when something is overdue or due within 3 days.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, dry_run=False, **opts):
        today = timezone.localdate()
        urgent_by = today + timedelta(days=digest.URGENT_DAYS)
        sent = 0
        prefs = {}
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
                    if user.pk not in prefs:
                        prefs[user.pk] = DigestPreference.frequency_for(user)
                    if not digest.is_due_day(prefs[user.pk], today):
                        continue
                    weekly = prefs[user.pk] == DigestPreference.WEEKLY
                    urgent, waiting, more = waiting_for(user, m, today, everything=weekly)
                    # The weekly summary goes whenever anything at all is waiting
                    urgent = urgent or (weekly and bool(waiting))
                    ctx = {'waiting': waiting, 'more': more,
                           'needs_me_link': notify.absolute(reverse('needs_me', args=[org.slug])),
                           'unsubscribe_link': notify.absolute(digest.unsubscribe_path(user)),
                           'weekly': weekly}
                    if m.is_internal:
                        mine_insurance = insurance if m.is_admin or m.role == Membership.ROLE_MEMBER else []
                        ctx.update({
                            # Estimators see tender returns; projects-only staff can't open bids
                            'tenders': tenders if m.can_estimate else [],
                            'insurance': mine_insurance,
                            'trial_days': trial_days if m.is_admin else None,
                        })
                        urgent = urgent or bool(ctx['tenders']) or (ctx['trial_days'] or 99) <= digest.URGENT_DAYS \
                            or any(s.next_insurance_expiry and s.next_insurance_expiry <= urgent_by for s in mine_insurance)
                    if urgent:
                        sent += self._send(user, org, ctx, dry_run)
        self.stdout.write(self.style.SUCCESS(f'{sent} digest(s) {"would be " if dry_run else ""}sent'))

    def _send(self, user, org, ctx, dry_run):
        if dry_run:
            self.stdout.write(f'  {org.slug}: {user.email}')
            return 1
        notify.send(user.email, 'Your weekly round-up' if ctx.get('weekly') else 'Your daily round-up', 'digest',
                    {'user': user, **ctx}, org=org)
        return 1
