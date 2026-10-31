"""
Fixes from the independent review of the procurement branch, one test (or more) per finding:
M1 forwards to several consultants, L1 statuses only move forward, L2 no staff names for
externals, L3 the consultant's neutral view, L4/L5 package choices, L6 reviewer fallback,
L7 two people submitting at once, L8 the overview link, L9 ordering with submittals in review,
L10 recalculating when the contract's bank holiday region changes, and the forward migrations.
"""
import shutil
import tempfile
from datetime import date
from unittest import mock

from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings

from changes.tests.base import make_user
from contracts.models import MainContract
from core.exceptions import InvalidTransition
from directory.models import Package
from projects.models import ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership

from .. import services
from ..models import ScheduleLine, Submittal, SubmittalFile, SubmittalForward
from ..workflow import reviewer_for
from .base import ProcurementTestBase, pdf

MEDIA = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=MEDIA)
class ForwardTwiceTests(ProcurementTestBase):
    """M1: the reviewer's repro, step by step."""

    def setUp(self):
        super().setUp()
        self.dan = make_user('dan@d.test', 'Dan Designer')
        Membership.objects.create(organization=self.org_a, user=self.dan, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.dan, role=ProjectMember.ROLE_CONSULTANT)

    def file_url(self, s, f):
        return self.url('submittal_file', self.pa.pk, s.pk, f.pk)

    def test_a_second_consultant_never_sees_or_overwrites_the_firsts_comments(self):
        s = self.submittal()
        self.login(self.sid)
        self.client.post(self.detail(s), {'action': 'submit', 'files': [pdf()]})                     # 1
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'forward', 'consultant': self.connie.pk})       # 2
        self.login(self.connie)
        self.client.post(self.detail(s), {'action': 'designer_reply', 'comments': 'Connie says brass',
                                          'files': [pdf('connie.pdf')]})                             # 3
        self.assertEqual(self.fresh(s).status, Submittal.SUBMITTED)
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'forward', 'consultant': self.dan.pk})          # 4
        with org_context(self.org_a):
            connie_file = SubmittalFile.objects.get(original_name='connie.pdf')
            self.assertEqual(connie_file.forward.consultant, self.connie)

        self.login(self.dan)
        page = self.client.get(self.detail(s))
        self.assertNotContains(page, 'Connie says brass')
        self.assertNotContains(page, 'connie.pdf')
        self.assertEqual(self.client.get(self.file_url(s, connie_file)).status_code, 404)
        self.login(self.connie)
        self.assertEqual(self.client.get(self.file_url(s, connie_file)).status_code, 200)
        self.assertContains(self.client.get(self.detail(s)), 'Connie says brass')

        self.login(self.dan)
        self.client.post(self.detail(s), {'action': 'designer_reply', 'comments': 'Dan says chrome'})
        with org_context(self.org_a):
            got = dict(SubmittalForward.objects.filter(revision__submittal=s).values_list('consultant', 'comments'))
        self.assertEqual(got, {self.connie.pk: 'Connie says brass', self.dan.pk: 'Dan says chrome'})
        self.login(self.pat)
        page = self.client.get(self.detail(s))
        self.assertContains(page, 'Connie says brass')
        self.assertContains(page, 'Dan says chrome')

    def test_asking_the_same_consultant_again_makes_a_new_forward(self):
        s = self.submitted()
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'forward', 'consultant': self.connie.pk})
        self.login(self.connie)
        self.client.post(self.detail(s), {'action': 'designer_reply', 'comments': 'First go'})
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'forward', 'consultant': self.connie.pk, 'note': 'Again please'})
        with org_context(self.org_a):
            rows = list(SubmittalForward.objects.filter(consultant=self.connie).order_by('pk')
                        .values_list('comments', 'replied_at'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 'First go')
        self.assertEqual(rows[1], ('', None))


@override_settings(MEDIA_ROOT=MEDIA)
class ReviewFixTests(ProcurementTestBase):
    def test_l1_status_only_moves_forward_in_the_edit_form(self):
        line = self.line(status=ScheduleLine.APPROVED)
        self.login(self.pat)
        page = self.client.get(self.url('procurement_line', self.pa.pk, line.pk))
        self.assertNotContains(page, 'value="NOT_STARTED"')
        self.assertContains(page, 'value="ORDERED"')
        r = self.client.post(self.url('procurement_line', self.pa.pk, line.pk), {
            'title': line.title, 'required_on_site': '2026-12-14', 'lead_time_weeks': '6', 'approval_weeks': '2',
            'status': 'NOT_STARTED'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fresh(line).status, ScheduleLine.APPROVED)

    def test_l2_externals_see_the_company_not_a_staff_name(self):
        s = self.submittal()
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'submit', 'files': [pdf()]})  # staff upload on the sub's behalf
        self.login(self.sid)
        page = self.client.get(self.detail(s))
        self.assertNotContains(page, 'Pat PM')
        self.assertContains(page, 'by Alpha Fitout')
        self.login(self.pat)
        self.assertContains(self.client.get(self.detail(s)), 'by Pat PM')

    def test_l3_consultant_sees_a_neutral_status_and_only_their_revision(self):
        s = self.submitted()
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'forward', 'consultant': self.connie.pk})
        self.login(self.connie)
        self.assertContains(self.client.get(self.detail(s)), 'With you')
        self.client.post(self.detail(s), {'action': 'designer_reply', 'comments': 'ok'})
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'review', 'code': 'C', 'comments': 'Revise'})
        self.login(self.sid)
        self.client.post(self.detail(s), {'action': 'submit', 'files': [pdf('rev1.pdf')]})
        self.login(self.connie)
        self.client.cookies.pop('messages', None)  # flash messages left over from the other logins in this client
        page = self.client.get(self.detail(s))
        self.assertContains(page, 'Comments sent')
        for hidden in ('In review', 'C: Revise', 'Ball in court', 'Rev 01', 'rev1.pdf', 'Sid Joinery</p>'):
            self.assertNotContains(page, hidden)
        listing = self.client.get(self.url('submittal_list', self.pa.pk))
        self.assertContains(listing, 'Comments sent')
        self.assertNotContains(listing, 'In review')

    def test_l4_a_package_that_has_since_lost_stays_on_the_forms(self):
        line = self.line(package=self.pkg)
        s = self.submittal(line=line)
        with org_context(self.org_a):
            self.pkg.status = Package.UNSUCCESSFUL
            self.pkg.save()
        self.login(self.pat)
        for url in (self.url('procurement_line', self.pa.pk, line.pk), self.url('submittal_edit', self.pa.pk, s.pk)):
            self.assertContains(self.client.get(url), f'value="{self.pkg.pk}" selected')

    def test_l5_submittals_only_on_appointed_packages(self):
        with org_context(self.org_a):
            quoted = Package.objects.create(project=self.pa, subcontractor=self.subco2, status=Package.QUOTED)
        self.login(self.pat)
        page = self.client.get(self.url('submittal_create', self.pa.pk))
        self.assertNotContains(page, f'value="{quoted.pk}"')
        r = self.client.post(self.url('submittal_create', self.pa.pk), {'title': 'X', 'kind': 'OTHER',
                                                                         'package': quoted.pk})
        self.assertEqual(r.status_code, 200)
        line = self.line(package=quoted)
        r = self.client.post(self.url('submittal_create', self.pa.pk), {'title': 'Y', 'kind': 'OTHER', 'line': line.pk})
        self.assertContains(r, "isn&#x27;t appointed")
        with org_context(self.org_a):
            self.assertFalse(Submittal.objects.filter(title__in=('X', 'Y')).exists())

    def test_l6_reviewer_falls_back_to_admins_never_the_bid_lead(self):
        with org_context(self.org_a):
            self.pa.owner = self.eve  # the bid lead
            self.pa.manager = None
            self.pa.save()
            self.assertEqual(reviewer_for(self.pa), self.alice)  # first owner/admin, not Eve
            Membership.objects.filter(organization=self.org_a, user=self.alice).update(is_active=False)
            self.assertEqual(reviewer_for(self.pa), self.adam)
            Membership.objects.filter(organization=self.org_a, user=self.adam).update(is_active=False)
            self.assertIsNone(reviewer_for(self.pa))
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('submittal_list', self.pa.pk)), 'Nobody would review')

    def test_l6_orders_with_no_pm_go_to_admins_not_the_bid_lead(self):
        import actions
        from datetime import timedelta

        from django.utils import timezone
        with org_context(self.org_a):
            self.pa.owner, self.pa.manager = self.pat, None
            self.pa.save()
        self.line(required_on_site=timezone.localdate() + timedelta(days=2), lead_time_weeks=0, approval_weeks=0)

        def orders(user):
            with org_context(self.org_a):
                return [i for i in actions.for_user(self.request_for(user)) if i.kind == 'order_by']
        self.assertEqual(orders(self.pat), [])
        self.assertEqual(len(orders(self.alice)), 1)
        self.assertEqual(len(orders(self.adam)), 1)

    def test_l7_a_second_submit_from_a_stale_copy_is_refused(self):
        s = self.submittal()
        stale = self.fresh(s)
        with org_context(self.org_a):
            services.submit(s, self.sid, [pdf()])
            with self.assertRaises(InvalidTransition):
                services.submit(stale, self.sid, [pdf()])
            self.assertEqual(s.revisions.count(), 1)

    def test_l7_integrity_error_shows_the_conflict_message(self):
        s = self.submittal()
        self.login(self.sid)
        with mock.patch('procurement.services.submit', side_effect=IntegrityError('dup')):
            r = self.client.post(self.detail(s), {'action': 'submit', 'files': [pdf()]}, follow=True)
        self.assertContains(r, 'Someone else changed this since you opened it')

    def test_l8_overview_link_says_packages(self):
        with org_context(self.org_a):
            self.pkg.quote_value = 1000
            self.pkg.save()
        self.login(self.pat)
        page = self.client.get(self.url('project_detail', self.pa.pk))
        self.assertNotContains(page, 'Procurement →')

    def test_l9_cannot_mark_ordered_with_submittals_in_review(self):
        line = self.line(package=self.pkg, status=ScheduleLine.IN_REVIEW)
        s = self.submitted(line=line)
        self.login(self.pat)
        r = self.client.post(self.url('procurement_line', self.pa.pk, line.pk), {'action': 'ordered'}, follow=True)
        self.assertContains(r, 'SUB-001 needs an A or B')
        self.assertEqual(self.fresh(line).status, ScheduleLine.IN_REVIEW)
        r = self.client.post(self.url('procurement_line', self.pa.pk, line.pk), {
            'title': line.title, 'package': self.pkg.pk, 'required_on_site': '2026-12-14', 'lead_time_weeks': '6',
            'approval_weeks': '2', 'status': 'ORDERED'})
        self.assertContains(r, 'still waiting for an A or B')
        self.client.post(self.detail(s), {'action': 'review', 'code': 'B', 'comments': 'ok'})
        self.client.post(self.url('procurement_line', self.pa.pk, line.pk), {'action': 'ordered'})
        self.assertEqual(self.fresh(line).status, ScheduleLine.ORDERED)

    def test_l10_changing_the_contract_region_recalculates_the_lines(self):
        with org_context(self.org_a):
            contract = MainContract.objects.create(project=self.pa, form='JCT_IC')
        line = self.line(required_on_site=date(2026, 1, 16), lead_time_weeks=2, approval_weeks=0)
        self.assertEqual(line.order_by, date(2026, 1, 2))
        with org_context(self.org_a):
            contract.bank_holiday_region = 'scotland'
            contract.save()
        self.assertEqual(self.fresh(line).order_by, date(2025, 12, 31))
        with org_context(self.org_b):
            self.assertEqual(ScheduleLine.objects.count(), 0)


class ForwardMigrationTests(TransactionTestCase):
    """procurement 0002-0004: single forward columns move into SubmittalForward rows, and back."""
    BEFORE = [('procurement', '0001_initial')]
    AFTER = [('procurement', '0004_remove_revision_forward_fields')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        self.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_forward_and_backward(self):
        from django.utils import timezone
        old = self.migrate(self.BEFORE)
        Org, Project, User = (old.get_model('tenancy', 'Organization'), old.get_model('projects', 'Project'),
                              old.get_model('accounts', 'User'))
        Sub, Rev, File = (old.get_model('procurement', 'Submittal'), old.get_model('procurement', 'SubmittalRevision'),
                          old.get_model('procurement', 'SubmittalFile'))
        org = Org.objects.create(name='Alpha', slug='alpha')
        pa = Project.objects.create(organization=org, number=1, name='Alpha job', stage='LIVE')
        pat = User.objects.create(email='pat@a.test', full_name='Pat', password='!')
        con = User.objects.create(email='con@a.test', full_name='Con', password='!')
        s = Sub.objects.create(organization=org, project=pa, number=1, ref='SUB-001', title='T', status='SUBMITTED')
        when = timezone.now()
        fwd_rev = Rev.objects.create(organization=org, submittal=s, number=0, forwarded_to=con, forwarded_by=pat,
                                     forwarded_at=when, forward_note='n', consultant_comments='c',
                                     consultant_replied_at=when)
        Rev.objects.create(organization=org, submittal=s, number=1)
        File.objects.create(organization=org, revision=fwd_rev, role='CONSULTANT', file='x/con.pdf')
        File.objects.create(organization=org, revision=fwd_rev, role='SUBMISSION', file='x/sub.pdf')

        new = self.migrate(self.AFTER)
        Forward, NewFile = new.get_model('procurement', 'SubmittalForward'), new.get_model('procurement', 'SubmittalFile')
        rows = list(Forward.objects.values_list('revision_id', 'consultant_id', 'forwarded_by_id', 'note', 'comments'))
        self.assertEqual(rows, [(fwd_rev.pk, con.pk, pat.pk, 'n', 'c')])
        fwd = Forward.objects.get()
        self.assertEqual(dict(NewFile.objects.values_list('file', 'forward_id')), {'x/con.pdf': fwd.pk, 'x/sub.pdf': None})

        back = self.migrate(self.BEFORE)
        r = back.get_model('procurement', 'SubmittalRevision').objects.get(pk=fwd_rev.pk)
        self.assertEqual((r.forwarded_to_id, r.forwarded_by_id, r.forward_note, r.consultant_comments),
                         (con.pk, pat.pk, 'n', 'c'))
        self.assertIsNone(back.get_model('procurement', 'SubmittalRevision').objects.get(number=1).forwarded_to_id)
