"""Programme (Gantt) and tender analytics."""
import io
import os
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import urlencode

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from tenancy.context import org_context
from tenancy.models import Membership
from tenancy.tests import Base, make_user

from . import gantt
from .analytics import tender_analytics, value_band
from .forms import ActivityForm
from .models import Activity, Document, DocumentRevision, FileAccess, FileScan, Project, ProjectMember


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
                quotes_due=self.today + timedelta(days=5), quality_due=self.today + timedelta(days=8),
                tender_due=timezone.now() + timedelta(days=10))
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
        self.assertContains(r, 'g-mark m-quality')
        self.assertNotContains(r, 'g-mark m-queries')
        self.assertNotContains(r, 'g-mark m-programme')
        self.assertContains(r, 'Missing dates')
        self.assertContains(r, 'Undated lead')
        row = next(row for row in r.context['chart']['rows'] if row['item'] == bid)
        self.assertEqual([m['kind'] for m in row['marks']], ['visit', 'quotes', 'quality'])
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


# ---------------------------------------------------------------------------
# File storage: signed cloud links, the download log, and moving files across
# ---------------------------------------------------------------------------

class SignedLinkStorage(FileSystemStorage):
    """Stands in for S3: keeps files on disk but hands out signed, expiring URLs."""
    def url(self, name, parameters=None, expire=None, http_method=None):
        return f'https://files.example/{name}?expires={expire}&' + urlencode(parameters or {})


def cloud(location, scan=False):
    return override_settings(STORAGE_BUCKET='test-bucket', STORAGE_MALWARE_SCAN=scan, STORAGES={
        'default': {'BACKEND': 'projects.tests.SignedLinkStorage', 'OPTIONS': {'location': location}},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    })


class FileStorageMixin:
    def setUp(self):
        super().setUp()
        self.local_dir, self.cloud_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.local_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.cloud_dir, ignore_errors=True)
        self.outsider = make_user('out@x.test', 'Olly Outsider')
        with org_context(self.org_a):
            Membership.objects.create(organization=self.org_a, user=self.outsider, role=Membership.ROLE_EXTERNAL)

    def add_doc(self):
        with org_context(self.org_a):
            doc = Document.objects.create(project=self.pa, title='GA plan', reference='A-100',
                                          visibility=Document.VIS_SHARED)
            rev = DocumentRevision.objects.create(document=doc, revision='C01', original_name='A-100.pdf', size=8,
                                                  file=SimpleUploadedFile('A-100.pdf', b'%PDF-1.4'))
        return doc, rev

    def download(self, doc, rev):
        return self.client.get(self.url('document_download', self.org_a, self.pa.pk, doc.pk, rev.pk))


class FileStorageTests(FileStorageMixin, Base):

    def test_local_disk_streams_and_logs(self):
        with self.settings(MEDIA_ROOT=self.local_dir):
            doc, rev = self.add_doc()
            self.login(self.alice)
            r = self.download(doc, rev)
            self.assertEqual(r.status_code, 200)
            log = FileAccess.all_objects.get()
            self.assertEqual((log.user, log.project, log.label), (self.alice, self.pa, 'A-100 rev C01'))

    def test_cloud_redirects_to_short_lived_signed_link(self):
        with cloud(self.cloud_dir):
            doc, rev = self.add_doc()
            self.login(self.alice)
            r = self.download(doc, rev)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r['Location'].startswith(f'https://files.example/{rev.file.name}?expires=300&'))
            self.assertIn('ResponseContentDisposition=inline', r['Location'])
            self.assertEqual(r['Cache-Control'], 'private, no-store')
            self.assertEqual(FileAccess.all_objects.count(), 1)

    def test_cloud_link_is_never_issued_without_permission(self):
        with cloud(self.cloud_dir):
            doc, rev = self.add_doc()
            self.login(self.outsider)   # in the company's portal, not on this project: it doesn't exist for them
            self.assertEqual(self.download(doc, rev).status_code, 404)
            self.login(self.bob)        # another company entirely
            r = self.client.get(self.url('document_download', self.org_b, self.pa.pk, doc.pk, rev.pk))
            self.assertEqual(r.status_code, 404)
            self.assertFalse(FileAccess.all_objects.exists())

    def test_download_log_shown_to_staff_only(self):
        with self.settings(MEDIA_ROOT=self.local_dir):
            doc, rev = self.add_doc()
            with org_context(self.org_a):
                ProjectMember.objects.create(project=self.pa, user=self.outsider, role=ProjectMember.ROLE_CLIENT)
            self.login(self.outsider)
            self.download(doc, rev)
            r = self.client.get(self.url('document_detail', self.org_a, self.pa.pk, doc.pk))
            self.assertNotContains(r, 'who opened which revision')
            self.login(self.alice)
            r = self.client.get(self.url('document_detail', self.org_a, self.pa.pk, doc.pk))
            self.assertContains(r, 'who opened which revision')
            self.assertContains(r, 'Olly Outsider')

    def test_move_files_to_storage_copies_once(self):
        with self.settings(MEDIA_ROOT=self.local_dir):
            doc, rev = self.add_doc()
        with cloud(self.cloud_dir), self.settings(MEDIA_ROOT=self.local_dir):
            call_command('move_files_to_storage', '--dry-run', stdout=io.StringIO())
            self.assertFalse(os.path.exists(os.path.join(self.cloud_dir, rev.file.name)))
            call_command('move_files_to_storage', stdout=io.StringIO())
            self.assertTrue(os.path.exists(os.path.join(self.cloud_dir, rev.file.name)))
            out = io.StringIO()
            call_command('move_files_to_storage', stdout=out)
            self.assertIn('Copied 0, already there 1', out.getvalue())


class MalwareScanTests(FileStorageMixin, Base):
    """Files in cloud storage are only handed out once GuardDuty has tagged them clean."""
    def scanned(self, verdict):
        return mock.patch('projects.files.fetch_scan_tag', return_value=verdict)

    def test_clean_file_is_served_and_the_verdict_cached(self):
        with cloud(self.cloud_dir, scan=True), self.scanned('NO_THREATS_FOUND') as fetch:
            doc, rev = self.add_doc()
            self.login(self.alice)
            self.assertEqual(self.download(doc, rev).status_code, 302)
            self.assertEqual(self.download(doc, rev).status_code, 302)
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(FileScan.all_objects.get(path=rev.file.name).is_clean)

    def test_unscanned_file_waits(self):
        with cloud(self.cloud_dir, scan=True), self.scanned(None):
            doc, rev = self.add_doc()
            self.login(self.alice)
            r = self.download(doc, rev)
            self.assertEqual(r.status_code, 409)
            self.assertContains(r, 'Virus check in progress', status_code=409)
            self.assertFalse(FileAccess.all_objects.exists())
            self.assertFalse(FileScan.all_objects.exists())

    def test_infected_file_blocked_and_staff_told_once(self):
        from tenancy.models import Notification
        with cloud(self.cloud_dir, scan=True), self.scanned('THREATS_FOUND'):
            doc, rev = self.add_doc()
            self.login(self.alice)
            r = self.download(doc, rev)
            self.assertEqual(r.status_code, 403)
            self.assertContains(r, 'contains a virus', status_code=403)
            self.download(doc, rev)
            self.assertEqual(Notification.all_objects.filter(user=self.alice, title__startswith='Infected').count(), 1)
            self.assertFalse(FileAccess.all_objects.exists())

    def test_unscannable_file_blocked(self):
        with cloud(self.cloud_dir, scan=True), self.scanned('UNSUPPORTED'):
            doc, rev = self.add_doc()
            self.login(self.alice)
            r = self.download(doc, rev)
            self.assertEqual(r.status_code, 403)
            self.assertContains(r, "couldn't be virus-checked", status_code=403)

    def test_scanning_can_be_switched_off(self):
        with cloud(self.cloud_dir, scan=False), self.scanned('THREATS_FOUND') as fetch:
            doc, rev = self.add_doc()
            self.login(self.alice)
            self.assertEqual(self.download(doc, rev).status_code, 302)
            fetch.assert_not_called()

    def test_scheduled_check_alerts_uploader_and_staff_once(self):
        from tenancy.models import Notification
        with cloud(self.cloud_dir, scan=True), self.scanned('THREATS_FOUND'):
            doc, rev = self.add_doc()
            DocumentRevision.all_objects.filter(pk=rev.pk).update(uploaded_by=self.outsider)
            call_command('check_file_scans', stdout=io.StringIO(), stderr=io.StringIO())
            call_command('check_file_scans', stdout=io.StringIO(), stderr=io.StringIO())
            self.assertEqual(FileScan.all_objects.get(path=rev.file.name).status, 'THREATS_FOUND')
            for user in (self.alice, self.outsider):
                self.assertEqual(Notification.all_objects.filter(user=user, title__startswith='Infected').count(), 1)

    def test_retrigger_asks_for_scans_of_unscanned_files(self):
        with cloud(self.cloud_dir, scan=True), self.scanned(None), \
                mock.patch('projects.management.commands.check_file_scans.retrigger_scan') as retrigger:
            doc, rev = self.add_doc()
            call_command('check_file_scans', stdout=io.StringIO())
            retrigger.assert_not_called()
            out = io.StringIO()
            call_command('check_file_scans', '--retrigger', stdout=out)
            self.assertEqual([c.args[0].name for c in retrigger.call_args_list], [rev.file.name])
            self.assertIn('Asked GuardDuty to scan 1 file', out.getvalue())


class CurrentRevisionTests(Base):
    """Which revision of a drawing is current: P01 < P02 < P10 < C01, whatever order they were uploaded in."""

    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = self.settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        with org_context(self.org_a):
            self.doc = Document.objects.create(project=self.pa, title='GA plan', reference='A-100')

    def upload(self, code, **kwargs):
        with org_context(self.org_a):
            return self.doc.add_revision(DocumentRevision(revision=code, original_name=f'{code}.pdf', size=8,
                                                          file=SimpleUploadedFile(f'{code}.pdf', b'%PDF-1.4')), **kwargs)

    def current(self):
        with org_context(self.org_a):
            return Document.objects.get(pk=self.doc.pk).current.revision

    def test_revision_rank_orders_p_before_c_numerically(self):
        from .models import revision_rank
        codes = ['C02', 'P10', 'c01', 'P02', 'P1', 'C10']
        self.assertEqual(sorted(codes, key=revision_rank), ['P1', 'P02', 'P10', 'c01', 'C02', 'C10'])
        self.assertIsNone(revision_rank('A'))
        self.assertIsNone(revision_rank('rev 3'))

    def test_newer_revision_becomes_current(self):
        self.upload('P01')
        self.upload('P02')
        self.upload('C01')
        self.assertEqual(self.current(), 'C01')
        self.assertEqual(DocumentRevision.all_objects.filter(document=self.doc, is_current=True).count(), 1)

    def test_p10_is_newer_than_p02(self):
        self.upload('P02')
        self.upload('P10')
        self.assertEqual(self.current(), 'P10')

    def test_older_upload_does_not_displace_current(self):
        self.upload('C01')
        late = self.upload('P03')
        self.assertFalse(late.is_current)
        self.assertEqual(self.current(), 'C01')

    def test_older_upload_marked_current_explicitly(self):
        self.upload('C01')
        self.upload('P03', make_current=True)
        self.assertEqual(self.current(), 'P03')
        self.assertEqual(DocumentRevision.all_objects.filter(document=self.doc, is_current=True).count(), 1)

    def test_odd_codes_fall_back_to_upload_order(self):
        self.upload('C02')
        self.upload('A')
        self.assertEqual(self.current(), 'A')
        self.upload('rev 3')
        self.assertEqual(self.current(), 'rev 3')

    def test_revise_view_keeps_newer_current_and_make_current_switches(self):
        self.upload('C02')
        self.login(self.alice)
        path = self.url('document_detail', self.org_a, self.pa.pk, self.doc.pk)
        r = self.client.post(path, {'revision': 'C01', 'file': SimpleUploadedFile('old.pdf', b'%PDF-1.4')}, follow=True)
        self.assertContains(r, 'which stays current')
        self.assertEqual(self.current(), 'C02')
        old = DocumentRevision.all_objects.get(document=self.doc, revision='C01')
        self.client.post(path, {'action': 'make_current', 'revision': old.pk})
        self.assertEqual(self.current(), 'C01')

    def test_revise_view_can_mark_older_upload_current(self):
        self.upload('C02')
        self.login(self.alice)
        path = self.url('document_detail', self.org_a, self.pa.pk, self.doc.pk)
        self.client.post(path, {'revision': 'C01', 'make_current': 'on',
                                'file': SimpleUploadedFile('old.pdf', b'%PDF-1.4')})
        self.assertEqual(self.current(), 'C01')

    def test_external_cannot_change_current_on_someone_elses_document(self):
        self.upload('C01')
        old = self.upload('P01')
        sub = make_user('sub@s.test', 'Sid Sub')
        with org_context(self.org_a):
            Membership.objects.create(organization=self.org_a, user=sub, role=Membership.ROLE_EXTERNAL)
            ProjectMember.objects.create(project=self.pa, user=sub, role=ProjectMember.ROLE_SUBCONTRACTOR)
            Document.objects.filter(pk=self.doc.pk).update(visibility=Document.VIS_SHARED)
        self.login(sub)
        r = self.client.post(self.url('document_detail', self.org_a, self.pa.pk, self.doc.pk),
                             {'action': 'make_current', 'revision': old.pk})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.current(), 'C01')

    def test_data_migration_flags_existing_revisions(self):
        from importlib import import_module
        from django.apps import apps
        migration = import_module('projects.migrations.0010_documentrevision_is_current')
        with org_context(self.org_a):
            other = Document.objects.create(project=self.pa, title='Odd', reference='X-1')
        for doc, codes in ((self.doc, ['P01', 'C02', 'P10', 'C01']), (other, ['C01', 'A', 'P02'])):
            for code in codes:
                with org_context(self.org_a):
                    DocumentRevision.objects.create(document=doc, revision=code, original_name='f.pdf',
                                                    file=SimpleUploadedFile('f.pdf', b'%PDF-1.4'))
        migration.flag_current(apps, None)
        flagged = dict(DocumentRevision.all_objects.filter(is_current=True).values_list('document_id', 'revision'))
        # All P/C: highest code wins even though C01 was uploaded last; any odd code: latest upload wins
        self.assertEqual(flagged, {self.doc.pk: 'C02', other.pk: 'P02'})
