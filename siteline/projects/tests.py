"""Programme (Gantt) and tender analytics."""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from tenancy.context import org_context
from tenancy.models import Membership
from tenancy.tests import Base, make_user

from . import gantt
from .analytics import tender_analytics, value_band
from .forms import ActivityForm
from .models import Activity, Project, ProjectMember


class ProgrammeTests(Base):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.sub_user = make_user('sub@s.test', 'Sid Sub')
        with org_context(self.org_a):
            Membership.objects.create(organization=self.org_a, user=self.sub_user, role=Membership.ROLE_EXTERNAL)
            ProjectMember.objects.create(project=self.pa, user=self.sub_user, role=ProjectMember.ROLE_SUBCONTRACTOR)
            self.strip = Activity.objects.create(project=self.pa, name='Strip out', start=self.today,
                                                 finish=self.today + timedelta(days=9))
            self.secret = Activity.objects.create(project=self.pa, name='Hidden float', start=self.today,
                                                  finish=self.today + timedelta(days=3), is_shared=False)
        with org_context(self.org_b):
            self.bravo_act = Activity.objects.create(project=self.pb, name='Bravo activity', start=self.today,
                                                     finish=self.today)

    def test_staff_see_programme_with_internal_lines(self):
        self.login(self.alice)
        r = self.client.get(self.url('programme', self.org_a, self.pa.pk))
        self.assertContains(r, 'Strip out')
        self.assertContains(r, 'Hidden float')
        self.assertNotContains(r, 'Bravo activity')

    def test_external_sees_only_shared_lines_and_cannot_edit(self):
        self.login(self.sub_user)
        r = self.client.get(self.url('programme', self.org_a, self.pa.pk))
        self.assertContains(r, 'Strip out')
        self.assertNotContains(r, 'Hidden float')
        self.assertNotContains(r, 'Add activity')
        r = self.client.post(self.url('programme', self.org_a, self.pa.pk),
                             {'name': 'Sneaky', 'start': self.today.isoformat()})
        self.assertEqual(r.status_code, 403)
        r = self.client.get(self.url('activity_edit', self.org_a, self.pa.pk, self.strip.pk))
        self.assertEqual(r.status_code, 403)

    def test_cannot_reach_other_orgs_activity(self):
        self.login(self.bob)
        r = self.client.get(self.url('activity_edit', self.org_b, self.pb.pk, self.strip.pk))
        self.assertEqual(r.status_code, 404)
        r = self.client.get(self.url('programme', self.org_b, self.pa.pk))
        self.assertEqual(r.status_code, 404)

    def test_add_edit_and_delete_activity(self):
        self.login(self.alice)
        start = self.today + timedelta(days=10)
        r = self.client.post(self.url('programme', self.org_a, self.pa.pk), {
            'name': 'Second fix', 'start': start.isoformat(), 'finish': (start + timedelta(days=4)).isoformat(),
            'progress': 0, 'after': self.strip.pk, 'is_shared': 'on'})
        self.assertEqual(r.status_code, 302)
        a = Activity.all_objects.get(name='Second fix')
        self.assertEqual((a.after, a.duration_days, a.organization), (self.strip, 5, self.org_a))
        r = self.client.post(self.url('activity_edit', self.org_a, self.pa.pk, a.pk), {
            'name': 'Second fix M&E', 'start': start.isoformat(), 'finish': start.isoformat(), 'progress': 50})
        a.refresh_from_db()
        self.assertEqual((a.name, a.progress), ('Second fix M&E', 50))
        self.client.post(self.url('activity_edit', self.org_a, self.pa.pk, a.pk), {'delete': '1'})
        self.assertFalse(Activity.all_objects.filter(pk=a.pk).exists())

    def test_form_rules(self):
        with org_context(self.org_a):
            f = ActivityForm({'name': 'Backwards', 'start': '2026-05-10', 'finish': '2026-05-01', 'progress': 0},
                             project=self.pa)
            self.assertIn('finish', f.errors)
            f = ActivityForm({'name': 'Handover', 'start': '2026-05-10', 'finish': '2026-06-01', 'progress': 0,
                              'is_milestone': 'on'}, project=self.pa)
            self.assertTrue(f.is_valid(), f.errors)
            self.assertEqual(f.cleaned_data['finish'], date(2026, 5, 10))
            f = ActivityForm({'name': 'Odd', 'start': '2026-05-10', 'progress': 150}, project=self.pa)
            self.assertIn('progress', f.errors)
        with org_context(self.org_a):
            other = Project.objects.create(name='Another job', stage=Project.STAGE_LIVE)
            f = ActivityForm({'name': 'X', 'start': '2026-05-10', 'progress': 0, 'after': self.strip.pk}, project=other)
            self.assertIn('after', f.errors)   # predecessor must be on the same project

    def test_late_and_out_of_sequence_flags(self):
        with org_context(self.org_a):
            late = Activity.objects.create(project=self.pa, name='Late', start=self.today - timedelta(days=5),
                                           finish=self.today - timedelta(days=1), progress=60)
            early = Activity.objects.create(project=self.pa, name='Early', start=self.today + timedelta(days=2),
                                            finish=self.today + timedelta(days=12), after=self.strip)
        self.assertTrue(late.is_late)
        self.assertFalse(self.strip.is_late)
        self.assertTrue(early.is_out_of_sequence)

    def test_workload_is_staff_only_and_shows_secured_jobs(self):
        with org_context(self.org_a):
            self.pa.start_date, self.pa.completion_date = self.today, self.today + timedelta(days=60)
            self.pa.save()
        self.login(self.sub_user)
        self.assertEqual(self.client.get(self.url('workload', self.org_a)).status_code, 403)
        self.login(self.alice)
        r = self.client.get(self.url('workload', self.org_a) + '?view=secured')
        self.assertContains(r, 'Alpha secret job')
        self.assertNotContains(r, 'Bravo job')

    def test_workload_tenders_chart(self):
        with org_context(self.org_a):
            bid = Project.objects.create(
                name='Hotel tender', stage=Project.STAGE_TENDERING, owner=self.alice,
                tender_received=self.today - timedelta(days=7), site_visit=self.today,
                quotes_due=self.today + timedelta(days=5), tender_due=timezone.now() + timedelta(days=10))
            Project.objects.create(name='Undated lead', stage=Project.STAGE_LEAD)
        with org_context(self.org_b):
            Project.objects.create(name='Bravo tender', stage=Project.STAGE_TENDERING,
                                   tender_due=timezone.now() + timedelta(days=3))
        self.login(self.alice)
        r = self.client.get(self.url('workload', self.org_a))
        self.assertContains(r, 'Hotel tender')
        self.assertNotContains(r, 'Bravo tender')
        self.assertContains(r, 'g-mark m-visit')
        self.assertContains(r, 'g-mark m-quotes')
        self.assertNotContains(r, 'g-mark m-queries')
        self.assertContains(r, 'Missing dates')
        self.assertContains(r, 'Undated lead')
        row = next(row for row in r.context['chart']['rows'] if row['item'] == bid)
        self.assertEqual(len(row['marks']), 2)
        r = self.client.get(self.url('workload', self.org_a) + f'?lead={self.bob.pk}')
        self.assertNotContains(r, 'Hotel tender')

    def test_project_form_date_checks(self):
        from .forms import ProjectForm
        with org_context(self.org_a):
            f = ProjectForm({'name': 'X', 'stage': 'TENDERING', 'tender_received': '2026-06-10',
                             'tender_due': '2026-06-01T12:00', 'start_date': '2026-08-01',
                             'completion_date': '2026-07-01'}, org=self.org_a)
            self.assertFalse(f.is_valid())
            self.assertIn('tender_received', f.errors)
            self.assertIn('completion_date', f.errors)


class GanttLayoutTests(Base):
    def test_bars_are_placed_on_a_monday_to_sunday_range(self):
        class A:
            def __init__(self, s, f):
                self.start, self.finish = s, f
        wed = date(2026, 3, 4)
        chart = gantt.build([A(wed, wed + timedelta(days=6)), A(wed + timedelta(days=7), wed + timedelta(days=7))],
                            today=wed)
        self.assertEqual(chart['start'], date(2026, 3, 2))
        self.assertEqual(chart['end'], date(2026, 3, 15))
        self.assertEqual(chart['rows'][0]['elapsed'], f'{1 / 7 * 100:.3f}')
        first, second = chart['rows']
        self.assertEqual(first['left'], f'{2 / 14 * 100:.3f}')
        self.assertEqual(first['width'], '50.000')
        self.assertEqual(second['left'], f'{9 / 14 * 100:.3f}')
        self.assertEqual(chart['today'], first['left'])
        self.assertEqual([t['label'] for t in chart['ticks']], ['2 Mar', '9 Mar'])

    def test_long_programmes_switch_to_month_labels(self):
        class A:
            start, finish = date(2026, 1, 5), date(2026, 12, 20)
        chart = gantt.build([A()], today=date(2025, 1, 1))
        self.assertIsNone(chart['today'])
        self.assertEqual(chart['ticks'][0]['label'], 'Feb 26')

    def test_marks_widen_the_range_and_sit_mid_day(self):
        class A:
            start, finish = date(2026, 3, 4), date(2026, 3, 6)
        chart = gantt.build([A()], marks=lambda i: [(date(2026, 3, 18), 'Visit', 'visit')], today=date(2026, 3, 4))
        self.assertEqual(chart['end'], date(2026, 3, 22))
        self.assertEqual(chart['rows'][0]['marks'][0]['left'], f'{16.5 / 21 * 100:.3f}')

    def test_nothing_dated_gives_no_chart(self):
        self.assertIsNone(gantt.build([]))


class TenderAnalyticsTests(Base):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        with org_context(self.org_a):
            for name, stage, value, sector, days in [
                ('Won retail', Project.STAGE_COMPLETE, '400000', 'RETAIL', 30),
                ('Won office', Project.STAGE_WON, '1200000', 'OFFICE', 60),
                ('Lost retail', Project.STAGE_LOST, '100000', 'RETAIL', 90),
                ('Lost retail 2', Project.STAGE_LOST, '30000', 'RETAIL', 100),
                ('Declined', Project.STAGE_DECLINED, '50000', 'RETAIL', 20),
                ('Old win', Project.STAGE_COMPLETE, '999999', 'OFFICE', 500),
            ]:
                p = Project.objects.create(name=name, stage=stage, value=Decimal(value), sector=sector,
                                           client_name='Kumo')
                p.decided_at = self.today - timedelta(days=days)
                p.save()
            Project.objects.create(name='Open bid', stage=Project.STAGE_TENDERING, value=Decimal('200000'), probability=50)
        with org_context(self.org_b):
            p = Project.objects.create(name='Bravo win', stage=Project.STAGE_WON, value=Decimal('5000000'))
            p.decided_at = self.today
            p.save()

    def test_win_rates_and_breakdowns(self):
        with org_context(self.org_a):
            d = tender_analytics(Project.objects.all(), '12', today=self.today)
        t = d['total']   # includes the base fixture's live 100k job, which counts as won
        self.assertEqual((t.won, t.lost, t.bids, d['declined']), (3, 2, 5, 1))
        self.assertEqual(t.win_rate, 60)
        self.assertEqual(t.won_value, Decimal('1700000'))
        self.assertEqual(t.value_win_rate, round(100 * 1700000 / 1830000))
        retail = next(r for r in d['by_sector'] if r.label == 'Retail')
        self.assertEqual((retail.bids, retail.won, retail.win_rate), (3, 1, 33))
        self.assertEqual([r.label for r in d['by_band']], ['Under 50k', '50k – 150k', '150k – 500k', '1m and over'])
        self.assertEqual(d['pipeline_weighted'], Decimal('100000'))
        self.assertEqual(sum(m['won'] + m['lost'] for m in d['monthly']), 5)
        self.assertEqual(len(d['monthly']), 12)

    def test_period_filter(self):
        with org_context(self.org_a):
            self.assertEqual(tender_analytics(Project.objects.all(), 'all', today=self.today)['total'].won, 4)

    def test_value_bands(self):
        self.assertEqual(value_band(Decimal('49999')), 'Under 50k')
        self.assertEqual(value_band(Decimal('1000000')), '1m and over')
        self.assertEqual(value_band(None), 'No value')

    def test_page_is_staff_only_and_tenant_scoped(self):
        self.login(self.alice)
        r = self.client.get(self.url('tender_analytics', self.org_a))
        self.assertContains(r, 'Tender analytics')
        self.assertContains(r, 'Lost retail')
        self.assertNotContains(r, 'Bravo win')
        ext = make_user('ext@x.test')
        with org_context(self.org_a):
            Membership.objects.create(organization=self.org_a, user=ext, role=Membership.ROLE_EXTERNAL)
            ProjectMember.objects.create(project=self.pa, user=ext, role=ProjectMember.ROLE_CLIENT)
        self.login(ext)
        self.assertEqual(self.client.get(self.url('tender_analytics', self.org_a)).status_code, 403)
