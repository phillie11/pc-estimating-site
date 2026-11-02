"""seed_demo coverage: the demo company has meetings, actions on Needs me, look-ahead lines for the joinery
subcontractor and Salford's programme imported from MS Project XML, and every page here opens for each demo login."""
import io
import tempfile
from types import SimpleNamespace

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

import actions
from accounts.models import User
from projects.models import Activity, Project, ProgrammeImport
from tenancy.context import org_context
from tenancy.models import Membership, Organization

from ..models import Meeting, MeetingAction


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='meetings-demo-'))
class DemoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', stdout=io.StringIO())
        cls.org = Organization.objects.get(slug='northbank-interiors')

    def who(self, email):
        return User.objects.get(email=email)

    def request_for(self, user):
        with org_context(self.org):
            m = Membership.objects.filter(organization=self.org, user=user, is_active=True).first()
        return SimpleNamespace(user=user, membership=m, org=self.org)

    def test_meetings_actions_and_needs_me(self):
        with org_context(self.org):
            leeds = Project.objects.get(name__startswith='Bar & kitchen')
            self.assertEqual(Meeting.objects.filter(project=leeds).count(), 4)
            self.assertEqual(Meeting.objects.filter(project=leeds, status=Meeting.STATUS_PLANNED).count(), 1)
            self.assertTrue(MeetingAction.objects.filter(project=leeds, status=MeetingAction.OPEN).exists())
            client_items = actions.for_user(self.request_for(self.who('client@demo.test')), kind='meeting_action')
            pm_items = actions.for_user(self.request_for(self.who('pm@demo.test')))
        self.assertEqual(len(client_items), 1)
        self.assertIn('veneer', client_items[0].title)
        refs = {i.ref[:4] for i in pm_items}
        self.assertTrue({'MTG-', 'ACT-'} <= refs, refs)  # minutes to issue, and an overdue action

    def test_programme_lines_for_the_sub_and_salford_import(self):
        with org_context(self.org):
            leeds = Project.objects.get(name__startswith='Bar & kitchen')
            salford = Project.objects.get(name='Office CAT A, Salford Quays')
            self.assertTrue(Activity.objects.filter(project=leeds, subcontractor__company_name='Lund Joinery Ltd').exists())
            self.assertEqual(ProgrammeImport.objects.get(project=salford).status, 'APPLIED')
            self.assertGreaterEqual(Activity.objects.filter(project=salford, source_uid__isnull=False).count(), 10)
        self.client.force_login(self.who('sub@demo.test'), backend='django.contrib.auth.backends.ModelBackend')
        r = self.client.get(reverse('programme_lookahead', args=[self.org.slug, leeds.pk]) + '?weeks=4')
        self.assertContains(r, 'Bar joinery')
        self.assertNotContains(r, 'Drylining &amp; ceilings')

    def test_every_page_opens_for_each_demo_login(self):
        with org_context(self.org):
            leeds = Project.objects.get(name__startswith='Bar & kitchen')
            issued = Meeting.objects.filter(project=leeds, status=Meeting.STATUS_ISSUED, kind='CLIENT').first()
        s, pk = self.org.slug, leeds.pk
        pages = {
            'pm@demo.test': [('meeting_list', pk), ('meeting_actions', pk), ('meeting_create', pk),
                             ('meeting_detail', pk, issued.pk), ('programme_lookahead', pk), ('programme_import', pk)],
            'owner@demo.test': [('meeting_list', pk), ('meeting_detail', pk, issued.pk)],
            'client@demo.test': [('meeting_list', pk), ('meeting_detail', pk, issued.pk), ('programme_lookahead', pk)],
            'sub@demo.test': [('meeting_list', pk), ('programme_lookahead', pk)],
        }
        for email, names in pages.items():
            self.client.force_login(self.who(email), backend='django.contrib.auth.backends.ModelBackend')
            for name, *args in names:
                with self.subTest(email=email, page=name):
                    self.assertEqual(self.client.get(reverse(name, args=[s, *args])).status_code, 200)
        self.client.force_login(self.who('owner@demo.test'), backend='django.contrib.auth.backends.ModelBackend')
        self.assertEqual(self.client.get(reverse('meeting_agendas', args=[s])).status_code, 200)
