"""
Procurement schedule and submittals: dates and RAG on saved lines, the A/B/C transitions and
resubmission revisions through the views, ball in court and Needs me for each role, who can
open what (staff, each subcontractor, the consultant, the client, bidders, other companies),
CSV export, the record index, and seed_demo.
"""
import io
import shutil
import tempfile
from datetime import date, timedelta

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

import actions
from contracts.models import MainContract
from core.access import visible_index
from core.models import AuditEntry, RecordIndex
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership

from .. import dates
from ..actions import OrderByProvider, SubmittalProvider
from ..models import ProcurementSettings, ScheduleLine, Submittal, SubmittalFile, SubmittalRevision
from .base import ProcurementTestBase, pdf

MEDIA = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=MEDIA)
class ScheduleLineTests(ProcurementTestBase):
    def test_dates_are_worked_out_on_save(self):
        line = self.line(required_on_site=date(2026, 6, 15), lead_time_weeks=6, approval_weeks=2)
        self.assertEqual((line.order_by, line.submittals_due), (date(2026, 4, 20), date(2026, 4, 2)))
        line.required_on_site = None
        line.save()
        self.assertEqual((line.order_by, line.submittals_due), (None, None))

    def test_dates_follow_the_contracts_bank_holiday_region(self):
        with org_context(self.org_a):
            MainContract.objects.create(project=self.pa, form='JCT_IC', bank_holiday_region='scotland')
        line = self.line(required_on_site=date(2026, 1, 16), lead_time_weeks=2, approval_weeks=0)
        self.assertEqual(line.order_by, date(2025, 12, 31))

    def test_rag_on_saved_lines_and_ordered_lines_are_done(self):
        today = timezone.localdate()
        red = self.line(required_on_site=today - timedelta(days=1), lead_time_weeks=0, approval_weeks=0)
        self.assertEqual(red.rag_on(today), dates.RED)
        green = self.line(required_on_site=today + timedelta(days=60), lead_time_weeks=1, approval_weeks=1)
        self.assertEqual(green.rag_on(today), dates.GREEN)
        red.status = ScheduleLine.ORDERED
        red.save()
        self.assertEqual(red.rag_on(today), dates.DONE)
        self.assertEqual(red.ordered_on, today)

    def test_advance_never_goes_backwards(self):
        line = self.line(status=ScheduleLine.ORDERED)
        with org_context(self.org_a):
            self.assertFalse(line.advance_to(ScheduleLine.APPROVED))
        self.assertEqual(self.fresh(line).status, ScheduleLine.ORDERED)

    def test_staff_add_edit_order_and_delete_through_the_views(self):
        self.login(self.pat)
        r = self.client.post(self.url('procurement_line_create', self.pa.pk), {
            'title': 'Kitchen extract', 'package': '', 'required_on_site': '2026-06-15', 'lead_time_weeks': '6',
            'approval_weeks': '2', 'status': 'NOT_STARTED'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            line = ScheduleLine.objects.get(title='Kitchen extract')
        self.assertEqual(line.order_by, date(2026, 4, 20))
        self.client.post(self.url('procurement_line', self.pa.pk, line.pk), {'action': 'ordered'})
        self.assertEqual(self.fresh(line).status, ScheduleLine.ORDERED)
        self.client.post(self.url('procurement_line', self.pa.pk, line.pk), {'action': 'delete'})
        with org_context(self.org_a):
            self.assertFalse(ScheduleLine.objects.filter(pk=line.pk).exists())

    def test_a_line_with_submittals_cannot_be_deleted(self):
        line = self.line()
        self.submittal(line=line)
        self.login(self.pat)
        self.client.post(self.url('procurement_line', self.pa.pk, line.pk), {'action': 'delete'})
        with org_context(self.org_a):
            self.assertTrue(ScheduleLine.objects.filter(pk=line.pk).exists())

    def test_schedule_filters_and_csv(self):
        today = timezone.localdate()
        self.line(title='Late item', required_on_site=today - timedelta(days=3), lead_time_weeks=0, approval_weeks=0)
        self.line(title='=HYPERLINK("x")', required_on_site=today + timedelta(days=100))
        self.login(self.pat)
        url = self.url('procurement_schedule', self.pa.pk)
        r = self.client.get(url, {'rag': 'red'})
        self.assertContains(r, 'Late item')
        self.assertNotContains(r, 'HYPERLINK')
        r = self.client.get(url, {'q': 'nothing like this'})
        self.assertContains(r, 'Nothing matches these filters')
        r = self.client.get(url, {'export': 'csv'})
        self.assertEqual(r['Content-Type'], 'text/csv')
        body = r.content.decode()
        self.assertIn('Order by', body.splitlines()[0])
        self.assertIn('Late item', body)
        self.assertIn("'=HYPERLINK", body)  # formulas are neutralised

    def test_empty_schedule_explains_itself(self):
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('procurement_schedule', self.pa.pk)), 'No procurement schedule yet')


@override_settings(MEDIA_ROOT=MEDIA)
class TransitionTests(ProcurementTestBase):
    def test_numbering_and_index(self):
        a, b = self.submittal(), self.submittal()
        other = self.submittal(project=self.pb, org=self.org_b, package=None, actor=self.bob)
        self.assertEqual((a.ref, b.ref, other.ref), ('SUB-001', 'SUB-002', 'SUB-001'))
        with org_context(self.org_a):
            idx = RecordIndex.objects.get(object_id=a.pk, record_type='procurement.submittal')
            self.assertTrue(idx.is_open)
            self.assertEqual(idx.ball_in_court_company_id, self.subco.pk)

    def test_sub_submits_review_c_then_resubmits_rev_01_then_a(self):
        line = self.line(package=self.pkg, status=ScheduleLine.APPOINTED)
        s = self.submittal(line=line)
        self.login(self.sid)
        r = self.client.post(self.detail(s), {'action': 'submit', 'files': [pdf('a.pdf'), pdf('b.pdf')],
                                              'note': 'First issue'})
        self.assertEqual(r.status_code, 302)
        s = self.fresh(s)
        today = timezone.localdate()
        self.assertEqual(s.status, Submittal.SUBMITTED)
        self.assertEqual(s.ball_in_court_user, self.pat)
        self.assertIsNone(s.ball_in_court_company)
        self.assertEqual(s.due_date, dates.add_working_days(today, 10))
        self.assertEqual(self.fresh(line).status, ScheduleLine.IN_REVIEW)
        with org_context(self.org_a):
            self.assertEqual(SubmittalFile.objects.filter(revision__submittal=s).count(), 2)

        # The sub can't review; C needs comments
        self.assertEqual(self.client.post(self.detail(s), {'action': 'review', 'code': 'A'}).status_code, 403)
        self.login(self.pat)
        r = self.client.post(self.detail(s), {'action': 'review', 'code': 'C', 'comments': ''})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fresh(s).status, Submittal.SUBMITTED)
        self.client.post(self.detail(s), {'action': 'review', 'code': 'C', 'comments': 'Show the LED channel',
                                          'markups': [pdf('markup.pdf')]})
        s = self.fresh(s)
        self.assertEqual(s.status, Submittal.REJECTED)
        self.assertEqual((s.ball_in_court_company, s.ball_in_court_user), (self.subco, None))
        self.assertEqual(s.due_date, dates.add_working_days(today, 10))

        self.login(self.sid)
        self.assertContains(self.client.get(self.detail(s)), 'Show the LED channel')
        self.client.post(self.detail(s), {'action': 'submit', 'files': [pdf('rev1.pdf')], 'note': 'LED added'})
        s = self.fresh(s)
        self.assertEqual((s.status, s.revision, s.revision_label), (Submittal.SUBMITTED, 1, '01'))
        with org_context(self.org_a):
            revs = list(SubmittalRevision.objects.filter(submittal=s).order_by('number'))
        self.assertEqual([(r.number, r.code) for r in revs], [(0, 'C'), (1, '')])  # full history kept

        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'review', 'code': 'A'})
        s = self.fresh(s)
        self.assertEqual(s.status, Submittal.APPROVED)
        self.assertIsNone(s.ball_in_court_user)
        self.assertIsNone(s.due_date)
        self.assertEqual(self.fresh(line).status, ScheduleLine.APPROVED)
        with org_context(self.org_a):
            actions_done = list(AuditEntry.objects.filter(record_idx__object_id=s.pk,
                                                          record_idx__record_type='procurement.submittal')
                                .order_by('pk').values_list('action', flat=True))
        self.assertEqual(actions_done, ['created', 'submit', 'reject', 'resubmit', 'approve'])

    def test_b_is_approved_with_comments_and_needs_them(self):
        line = self.line(package=self.pkg)
        s = self.submitted(line=line)
        self.login(self.pat)
        self.assertEqual(self.client.post(self.detail(s), {'action': 'review', 'code': 'B'}).status_code, 200)
        self.client.post(self.detail(s), {'action': 'review', 'code': 'B', 'comments': 'Use matt finish'})
        self.assertEqual(self.fresh(s).status, Submittal.APPROVED_COMMENTS)
        self.assertEqual(self.fresh(line).status, ScheduleLine.APPROVED)

    def test_line_waits_until_every_submittal_on_it_is_approved(self):
        line = self.line(package=self.pkg)
        s1, s2 = self.submitted(line=line), self.submitted(line=line)
        self.login(self.pat)
        self.client.post(self.detail(s1), {'action': 'review', 'code': 'A'})
        self.assertEqual(self.fresh(line).status, ScheduleLine.IN_REVIEW)  # s2 still in review
        self.client.post(self.detail(s2), {'action': 'review', 'code': 'B', 'comments': 'ok'})
        self.assertEqual(self.fresh(line).status, ScheduleLine.APPROVED)

    def test_an_ordered_line_stays_ordered_when_a_submittal_is_approved(self):
        line = self.line(package=self.pkg, status=ScheduleLine.ORDERED)
        s = self.submitted(line=line)
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'review', 'code': 'A'})
        self.assertEqual(self.fresh(line).status, ScheduleLine.ORDERED)

    def test_review_period_is_configurable_per_job(self):
        self.login(self.pat)
        self.client.post(self.url('procurement_settings', self.pa.pk), {'review_days': '5', 'resubmit_days': '3',
                                                                         'reviewer': self.adam.pk})
        with org_context(self.org_a):
            self.assertEqual(ProcurementSettings.for_project(self.pa).review_days, 5)
        s = self.submittal()
        self.login(self.sid)
        self.client.post(self.detail(s), {'action': 'submit', 'files': [pdf()]})
        s = self.fresh(s)
        today = timezone.localdate()
        self.assertEqual(s.due_date, dates.add_working_days(today, 5))
        self.assertEqual(s.ball_in_court_user, self.adam)  # the chosen reviewer, not the PM
        self.login(self.adam)
        self.client.post(self.detail(s), {'action': 'review', 'code': 'C', 'comments': 'x'})
        self.assertEqual(self.fresh(s).due_date, dates.add_working_days(today, 3))

    def test_forward_to_the_designer_and_back(self):
        s = self.submitted()
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'forward', 'consultant': self.connie.pk, 'note': 'Finish ok?'})
        s = self.fresh(s)
        self.assertEqual((s.status, s.ball_in_court_user), (Submittal.WITH_CONSULTANT, self.connie))
        due = s.due_date
        # Only consultants on the job can be picked
        s2 = self.submitted()
        r = self.client.post(self.detail(s2), {'action': 'forward', 'consultant': self.carla.pk})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fresh(s2).status, Submittal.SUBMITTED)

        self.login(self.connie)
        self.assertContains(self.client.get(self.detail(s)), 'Finish ok?')
        self.client.post(self.detail(s), {'action': 'designer_reply', 'comments': 'Prefer brushed brass',
                                          'files': [pdf('designer.pdf')]})
        s = self.fresh(s)
        self.assertEqual((s.status, s.ball_in_court_user, s.due_date), (Submittal.SUBMITTED, self.pat, due))
        # The consultant can't give the final status
        self.assertEqual(self.client.post(self.detail(s), {'action': 'review', 'code': 'A'}).status_code, 403)
        self.login(self.pat)
        self.assertContains(self.client.get(self.detail(s)), 'Prefer brushed brass')
        self.client.post(self.detail(s), {'action': 'review', 'code': 'B', 'comments': 'Brushed brass please'})
        self.assertEqual(self.fresh(s).status, Submittal.APPROVED_COMMENTS)

    def test_staff_can_decide_without_waiting_for_the_designer(self):
        s = self.submitted()
        self.login(self.pat)
        self.client.post(self.detail(s), {'action': 'forward', 'consultant': self.connie.pk})
        self.client.post(self.detail(s), {'action': 'review', 'code': 'A'})
        self.assertEqual(self.fresh(s).status, Submittal.APPROVED)

    def test_wrong_status_actions_are_refused(self):
        s = self.submittal()  # REQUIRED: nothing to review yet
        self.login(self.pat)
        self.assertEqual(self.client.post(self.detail(s), {'action': 'review', 'code': 'A'}).status_code, 403)
        self.assertEqual(self.client.post(self.detail(s), {'action': 'forward', 'consultant': self.connie.pk})
                         .status_code, 403)
        self.client.post(self.detail(s), {'action': 'withdraw'})
        s = self.fresh(s)
        self.assertEqual((s.status, s.is_open), (Submittal.WITHDRAWN, False))
        self.login(self.sid)
        self.assertEqual(self.client.post(self.detail(s), {'action': 'submit', 'files': [pdf()]}).status_code, 403)

    def test_direct_order_submittal_waits_on_the_reviewer_who_uploads_it(self):
        line = self.line(supplier='Lumen Lab')
        self.login(self.pat)
        self.client.post(self.url('submittal_create', self.pa.pk), {
            'title': 'Pendant samples', 'kind': 'SAMPLE', 'line': line.pk, 'package': ''})
        with org_context(self.org_a):
            s = Submittal.objects.get(title='Pendant samples')
        self.assertEqual((s.ball_in_court_user, s.ball_in_court_company, s.due_date),
                         (self.pat, None, line.submittals_due))
        self.client.post(self.detail(s), {'action': 'submit', 'files': [pdf()]})
        self.assertEqual(self.fresh(s).status, Submittal.SUBMITTED)

    def test_create_takes_the_package_from_the_line_and_tells_the_sub(self):
        from django.core import mail
        line = self.line(package=self.pkg)
        self.login(self.pat)
        self.client.post(self.url('submittal_create', self.pa.pk), {
            'title': 'Joinery samples', 'kind': 'SAMPLE', 'line': line.pk, 'package': ''})
        with org_context(self.org_a):
            s = Submittal.objects.get(title='Joinery samples')
        self.assertEqual((s.package, s.ball_in_court_company), (self.pkg, self.subco))
        self.assertTrue(any('sid@s.test' in m.to for m in mail.outbox))

    def test_needs_a_line_or_a_package(self):
        self.login(self.pat)
        r = self.client.post(self.url('submittal_create', self.pa.pk), {'title': 'Floating', 'kind': 'OTHER'})
        self.assertContains(r, 'Link it to a procurement line or a trade package')

    def test_sub_starts_a_submittal_from_a_line_on_their_package(self):
        line = self.line(package=self.pkg)
        self.line(title='Ceilings', package=self.pkg2)
        self.login(self.sid)
        page = self.client.get(self.url('submittal_list', self.pa.pk))
        self.assertContains(page, 'Bar joinery')
        self.assertNotContains(page, 'Ceilings')  # Sue's line
        r = self.client.post(self.url('submittal_list', self.pa.pk), {
            'action': 'start', 'line': line.pk, 'kind': 'PRODUCT_DATA', f'l{line.pk}-files': [pdf()],
            f'l{line.pk}-note': 'Data sheets'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            s = Submittal.objects.get(line=line)
        self.assertEqual((s.status, s.package, s.kind, s.created_by), (Submittal.SUBMITTED, self.pkg, 'PRODUCT_DATA',
                                                                        self.sid))

    def test_sub_cannot_start_one_on_another_subs_line(self):
        other = self.line(title='Ceilings', package=self.pkg2)
        self.login(self.sid)
        r = self.client.post(self.url('submittal_list', self.pa.pk), {
            'action': 'start', 'line': other.pk, f'l{other.pk}-files': [pdf()]})
        self.assertEqual(r.status_code, 404)
        with org_context(self.org_a):
            self.assertFalse(Submittal.objects.filter(line=other).exists())

    def test_blocked_file_types_are_refused(self):
        s = self.submittal()
        self.login(self.sid)
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = self.client.post(self.detail(s), {'action': 'submit', 'files': [SimpleUploadedFile('x.exe', b'MZ')]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fresh(s).status, Submittal.REQUIRED)


@override_settings(MEDIA_ROOT=MEDIA)
class NeedsMeTests(ProcurementTestBase):
    def kinds_for(self, user, kind=None):
        req = self.request_for(user)
        with org_context(self.org_a):
            items = actions.for_user(req)
        return [(i.kind, i.ref or i.title, i.due) for i in items if kind is None or i.kind == kind]

    def test_ball_in_court_and_needs_me_follow_the_submittal(self):
        s = self.submittal(due_date=timezone.localdate() + timedelta(days=5))
        # To submit: the subcontractor's company, nobody else
        self.assertEqual(self.kinds_for(self.sid, 'submittal'), [('submittal', 'SUB-001', s.due_date)])
        for other in (self.sue, self.pat, self.connie, self.carla, self.bea):
            self.assertEqual(self.kinds_for(other, 'submittal'), [], other)
        with org_context(self.org_a):
            from ..workflow import TRANSITIONS  # noqa: F401 (registered)
            SubmittalRevision.objects.create(submittal=s, number=0, submitted_by=self.sid)
            from core.workflow import apply_transition
            apply_transition(s, 'submit', self.sid)
        s = self.fresh(s)
        self.assertEqual(self.kinds_for(self.pat, 'submittal'), [('submittal', 'SUB-001', s.due_date)])
        self.assertEqual(self.kinds_for(self.sid, 'submittal'), [])
        with org_context(self.org_a):
            rev = s.latest
            rev.forwarded_to = self.connie
            rev.save()
            apply_transition(s, 'forward', self.pat, consultant=self.connie)
        self.assertEqual(len(self.kinds_for(self.connie, 'submittal')), 1)
        self.assertEqual(self.kinds_for(self.pat, 'submittal'), [])
        with org_context(self.org_a):
            apply_transition(s, 'designer_reply', self.connie)
            apply_transition(s, 'reject', self.pat)
        s = self.fresh(s)
        self.assertEqual(self.kinds_for(self.sid, 'submittal'), [('submittal', 'SUB-001', s.due_date)])
        self.assertEqual(self.kinds_for(self.connie, 'submittal'), [])
        # Not listed twice through the record index
        self.assertEqual(len([k for k in self.kinds_for(self.sid) if k[1] == 'SUB-001']), 1)

    def test_a_sub_whose_package_is_no_longer_appointed_gets_nothing(self):
        self.submittal()
        with org_context(self.org_a):
            self.pkg.status = 'UNSUCCESSFUL'
            self.pkg.save()
        self.assertEqual(self.kinds_for(self.sid, 'submittal'), [])

    def test_order_by_items_go_to_the_project_manager_within_7_days(self):
        today = timezone.localdate()
        soon = self.line(title='Soon', required_on_site=today + timedelta(days=5), lead_time_weeks=0,
                         approval_weeks=0)
        self.line(title='Late', required_on_site=today - timedelta(days=5), lead_time_weeks=0, approval_weeks=0)
        self.line(title='Later', required_on_site=today + timedelta(days=30), lead_time_weeks=0, approval_weeks=0)
        self.line(title='Done', required_on_site=today + timedelta(days=2), lead_time_weeks=0, approval_weeks=0,
                  status=ScheduleLine.ORDERED)
        titles = {t for _, t, _ in self.kinds_for(self.pat, 'order_by')}
        self.assertEqual(titles, {'Order Soon', 'Order Late'})
        due = [d for _, t, d in self.kinds_for(self.pat, 'order_by') if t == 'Order Soon'][0]
        self.assertEqual(due, soon.order_by)
        for other in (self.adam, self.alice, self.sid, self.carla, self.connie):
            self.assertEqual(self.kinds_for(other, 'order_by'), [], other)

    def test_providers_fail_closed_and_stay_in_their_company(self):
        self.submitted()
        self.line(required_on_site=timezone.localdate(), lead_time_weeks=0, approval_weeks=0)
        req = self.request_for(self.pat)
        for provider in (SubmittalProvider(), OrderByProvider()):
            with self.assertRaises(TenantContextMissing):
                list(provider.items(req))
        # Bob (another company) sees nothing of Alpha's
        with org_context(self.org_b):
            req_b = self.request_for(self.bob, org=self.org_b)
            self.assertEqual(SubmittalProvider().items(req_b), [])
            self.assertEqual(OrderByProvider().items(req_b), [])

    def test_decisions_tab_shows_the_subs_submittals(self):
        self.submittal(due_date=timezone.localdate() + timedelta(days=3))
        self.login(self.sid)
        r = self.client.get(self.url('project_decisions', self.pa.pk))
        self.assertContains(r, 'Bar shop drawings')
        self.assertContains(r, 'Upload it for review')
        self.assertContains(r, 'Submittals')  # the sub-tab under Decisions


@override_settings(MEDIA_ROOT=MEDIA)
class AccessTests(ProcurementTestBase):
    def setUp(self):
        super().setUp()
        self.mine = self.submitted(title='Sid drawings')
        with org_context(self.org_a):
            rev = self.mine.latest
            self.mine_file = SubmittalFile.objects.create(revision=rev, file=pdf('sid.pdf'), original_name='sid.pdf')
            self.markup = SubmittalFile.objects.create(revision=rev, file=pdf('m.pdf'), original_name='m.pdf',
                                                       role=SubmittalFile.MARKUP)
            self.designer_file = SubmittalFile.objects.create(revision=rev, file=pdf('d.pdf'), original_name='d.pdf',
                                                              role=SubmittalFile.CONSULTANT)
        self.sues = self.submittal(title='Sue tiles', package=self.pkg2)
        self.direct = self.submittal(title='Direct pendants', package=None)

    def get(self, user, name, *args):
        return self.status_for(user, self.url(name, self.pa.pk, *args))

    def test_staff_open_everything(self):
        for user in (self.alice, self.adam, self.pat):
            for name, args in (('procurement_schedule', ()), ('submittal_list', ()), ('submittal_create', ()),
                               ('procurement_line_create', ()), ('procurement_settings', ()),
                               ('submittal_detail', (self.mine.pk,)), ('submittal_detail', (self.direct.pk,)),
                               ('submittal_file', (self.mine.pk, self.designer_file.pk))):
                self.assertIn(self.get(user, name, *args), (200, 302), (user, name))

    def test_estimators_cannot_open_a_live_job(self):
        self.assertEqual(self.get(self.eve, 'procurement_schedule'), 404)
        self.assertEqual(self.get(self.eve, 'submittal_detail', self.mine.pk), 404)

    def test_client_sees_nothing(self):
        for name, args in (('procurement_schedule', ()), ('submittal_list', ()),
                           ('submittal_detail', (self.mine.pk,)), ('submittal_file', (self.mine.pk, self.mine_file.pk))):
            self.assertEqual(self.get(self.carla, name, *args), 403, name)
        self.login(self.carla)
        self.assertNotContains(self.client.get(self.url('project_detail', self.pa.pk)), 'Submittals')

    def test_bidders_and_outsiders_are_refused(self):
        self.assertEqual(self.get(self.bea, 'submittal_list'), 403)  # tendering sub: their tender page only
        self.assertEqual(self.get(self.bea, 'procurement_schedule'), 403)
        self.assertEqual(self.get(self.nora, 'submittal_list'), 404)  # not on the job at all
        self.assertEqual(self.get(self.nora, 'procurement_schedule'), 404)

    def test_sub_sees_only_their_own_appointed_package(self):
        self.assertEqual(self.get(self.sid, 'procurement_schedule'), 403)
        self.assertEqual(self.get(self.sid, 'submittal_create'), 403)
        self.assertEqual(self.get(self.sid, 'submittal_edit', self.mine.pk), 403)
        self.login(self.sid)
        r = self.client.get(self.url('submittal_list', self.pa.pk))
        self.assertContains(r, 'Sid drawings')
        self.assertNotContains(r, 'Sue tiles')
        self.assertNotContains(r, 'Direct pendants')
        self.assertEqual(self.get(self.sid, 'submittal_detail', self.mine.pk), 200)
        self.assertEqual(self.get(self.sid, 'submittal_detail', self.sues.pk), 404)
        self.assertEqual(self.get(self.sid, 'submittal_detail', self.direct.pk), 404)
        self.assertEqual(self.get(self.sue, 'submittal_detail', self.mine.pk), 404)
        self.assertEqual(self.get(self.sue, 'submittal_file', self.mine.pk, self.mine_file.pk), 404)
        self.assertEqual(self.get(self.sue, 'submittal_detail', self.sues.pk), 200)

    def test_sub_files_exclude_the_designers_markup(self):
        self.assertEqual(self.get(self.sid, 'submittal_file', self.mine.pk, self.mine_file.pk), 200)
        self.assertEqual(self.get(self.sid, 'submittal_file', self.mine.pk, self.markup.pk), 200)
        self.assertEqual(self.get(self.sid, 'submittal_file', self.mine.pk, self.designer_file.pk), 404)
        # A file id from another submittal through this one's URL
        with org_context(self.org_a):
            SubmittalRevision.objects.create(submittal=self.sues, number=0)
            sue_file = SubmittalFile.objects.create(revision=self.sues.latest, file=pdf('s.pdf'), original_name='s.pdf')
        self.assertEqual(self.get(self.sid, 'submittal_file', self.mine.pk, sue_file.pk), 404)

    def test_a_sub_loses_access_when_the_package_is_no_longer_appointed(self):
        with org_context(self.org_a):
            self.pkg.status = 'UNSUCCESSFUL'
            self.pkg.save()
        self.assertEqual(self.get(self.sid, 'submittal_detail', self.mine.pk), 404)

    def test_consultant_sees_only_what_was_forwarded(self):
        self.assertEqual(self.get(self.connie, 'procurement_schedule'), 403)
        self.assertEqual(self.get(self.connie, 'submittal_detail', self.mine.pk), 404)
        self.login(self.pat)
        self.client.post(self.detail(self.mine), {'action': 'forward', 'consultant': self.connie.pk})
        self.login(self.connie)
        r = self.client.get(self.url('submittal_list', self.pa.pk))
        self.assertContains(r, 'Sid drawings')
        self.assertNotContains(r, 'Sue tiles')
        self.assertEqual(self.get(self.connie, 'submittal_detail', self.mine.pk), 200)
        self.assertEqual(self.get(self.connie, 'submittal_detail', self.sues.pk), 404)
        self.assertEqual(self.get(self.connie, 'submittal_file', self.mine.pk, self.mine_file.pk), 200)
        self.assertEqual(self.get(self.connie, 'submittal_file', self.mine.pk, self.markup.pk), 404)
        # A revision that wasn't forwarded stays hidden, even on a submittal they can open
        with org_context(self.org_a):
            later = SubmittalRevision.objects.create(submittal=self.mine, number=1)
            later_file = SubmittalFile.objects.create(revision=later, file=pdf('l.pdf'), original_name='l.pdf')
        self.assertEqual(self.get(self.connie, 'submittal_file', self.mine.pk, later_file.pk), 404)

    def test_externals_never_see_staff_names_as_ball_in_court(self):
        self.login(self.sid)
        r = self.client.get(self.detail(self.mine))
        self.assertContains(r, 'Ball in court:</strong> Alpha Fitout')
        self.assertNotContains(r, 'Pat PM')

    def test_the_record_index_never_shows_submittals_to_externals(self):
        for user in (self.sid, self.connie, self.carla):
            req = self.request_for(user)
            req.project_member = None
            with org_context(self.org_a):
                self.assertFalse(visible_index(req).filter(record_type='procurement.submittal').exists(), user)

    def test_tabs(self):
        self.login(self.pat)
        r = self.client.get(self.url('project_detail', self.pa.pk))
        self.assertContains(r, self.url('procurement_schedule', self.pa.pk))
        self.login(self.sid)
        self.assertContains(self.client.get(self.url('project_decisions', self.pa.pk)),
                            self.url('submittal_list', self.pa.pk))

    def test_archived_project_is_read_only(self):
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.url('procurement_schedule', self.pa.pk)).status_code, 200)
        self.assertEqual(self.client.post(self.detail(self.mine), {'action': 'review', 'code': 'A'}).status_code, 403)
        self.assertEqual(self.fresh(self.mine).status, Submittal.SUBMITTED)


@override_settings(MEDIA_ROOT=MEDIA)
class TenancyTests(ProcurementTestBase):
    def test_another_company_gets_a_404(self):
        line = self.line()
        s = self.submitted()
        for name, args in (('procurement_schedule', ()), ('submittal_list', ()), ('procurement_line', (line.pk,)),
                           ('submittal_detail', (s.pk,))):
            self.assertEqual(self.status_for(self.bob, self.url(name, self.pa.pk, *args)), 404, name)
            # Bob in his own workspace, asking for Alpha's ids
            self.assertEqual(self.status_for(self.bob, self.url(name, self.pa.pk, *args, org=self.org_b)), 404, name)
        with org_context(self.org_b):
            Membership.objects.create(organization=self.org_b, user=self.pat, role=Membership.ROLE_MEMBER)
        self.assertEqual(self.status_for(self.pat, self.url('submittal_detail', self.pb.pk, s.pk, org=self.org_b)), 404)

    def test_no_organisation_fails_closed(self):
        self.line()
        self.submitted()
        for model in (ScheduleLine, Submittal, SubmittalRevision, SubmittalFile, ProcurementSettings):
            with self.assertRaises(TenantContextMissing):
                model.objects.count()

    def test_lines_and_submittals_stay_in_their_company(self):
        self.line()
        self.submitted()
        with org_context(self.org_b):
            self.assertEqual((ScheduleLine.objects.count(), Submittal.objects.count()), (0, 0))


class SeedDemoTests(TestCase):
    def test_demo_has_the_schedule_and_submittals(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            call_command('seed_demo', stdout=io.StringIO())
        from accounts.models import User
        from projects.models import Project
        from tenancy.models import Organization
        org = Organization.objects.get(slug='northbank-interiors')
        with org_context(org):
            leeds = Project.objects.get(name='Bar & kitchen fit-out, Call Lane')
            lines = list(ScheduleLine.objects.filter(project=leeds))
            self.assertEqual(len(lines), 8)
            rags = [x.rag for x in lines]
            self.assertEqual((rags.count('red'), rags.count('amber')), (1, 2))
            subs = {s.title: s for s in Submittal.objects.filter(project=leeds)}
            self.assertEqual(subs['Back-of-house joinery samples'].status, Submittal.REQUIRED)
            self.assertEqual(subs['Bar back and bar front shop drawings'].status, Submittal.REJECTED)
            self.assertEqual(subs['Feature pendant samples'].status, Submittal.WITH_CONSULTANT)
            self.assertTrue(ScheduleLine.objects.filter(project__name='Office CAT A, Salford Quays').exists())

            def items(email, kind):
                user = User.objects.get(email=email)
                m = Membership.objects.get(organization=org, user=user)
                return [i for i in actions.for_user(actions.request_for(user, m)) if i.kind == kind]
            self.assertEqual(len(items('sub@demo.test', 'submittal')), 2)
            self.assertEqual(len(items('designer@demo.test', 'submittal')), 1)
            self.assertGreaterEqual(len(items('pm@demo.test', 'submittal')), 1)
            self.assertEqual(len(items('pm@demo.test', 'order_by')), 2)
            self.assertEqual(items('client@demo.test', 'submittal'), [])
