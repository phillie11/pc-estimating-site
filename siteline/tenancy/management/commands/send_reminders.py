"""
Daily digest. Schedule on PythonAnywhere (Tasks tab), e.g. 07:00 daily:
    cd ~/siteline && source .venv/bin/activate && python manage.py send_reminders
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from directory.models import Subcontractor
from projects.models import Project
from rfis.models import RFI
from tenancy import notify
from tenancy.context import org_context
from tenancy.models import Membership, Organization


class Command(BaseCommand):
    help = 'Email each internal user a digest of overdue RFIs, tender deadlines, insurance expiry and trial status.'

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
                for m in org.memberships.filter(is_active=True).select_related('user'):
                    user = m.user
                    rfis = list(RFI.objects.filter(ball_in_court_user=user, status=RFI.STATUS_OPEN, due_date__lt=today,
                                                   project__archived_at__isnull=True)
                                .select_related('project'))
                    if not m.is_internal:
                        if rfis:
                            sent += self._send(user, org, {'rfis': rfis}, dry_run)
                        continue
                    ctx = {'rfis': rfis, 'tenders': tenders, 'insurance': insurance if m.is_admin or m.role == Membership.ROLE_MEMBER else [],
                           'trial_days': trial_days if m.is_admin else None}
                    if ctx['rfis'] or ctx['tenders'] or ctx['insurance'] or ctx['trial_days'] is not None:
                        sent += self._send(user, org, ctx, dry_run)
        self.stdout.write(self.style.SUCCESS(f'{sent} digest(s) {"would be " if dry_run else ""}sent'))

    def _send(self, user, org, ctx, dry_run):
        if dry_run:
            self.stdout.write(f'  {org.slug}: {user.email}')
            return 1
        notify.send(user.email, 'Your daily round-up', 'digest', {'user': user, **ctx}, org=org)
        return 1
