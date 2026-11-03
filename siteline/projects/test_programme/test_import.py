"""
MS Project XML import: the parser against hand-written MSPDI fixtures (fixtures/), refusing anything
that isn't MSPDI with a plain-English reason, the size cap, safe XML (no DTDs or entities), and the
preview -> apply flow through the views: re-importing updates by unique ID and never duplicates, hand
lines are left alone, who may import, other companies get a 404 and no organisation fails closed.
"""
import io
import os
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from changes.tests.base import CommercialTestBase
from directory.models import Package, Trade
from projects import mspdi, programme_import
from projects.models import Activity, ProgrammeImport
from tenancy.context import TenantContextMissing, org_context

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def fixture(name):
    with open(os.path.join(FIXTURES, name), 'rb') as f:
        return f.read()


def upload(name, data=None, as_name=None):
    return SimpleUploadedFile(as_name or name, data if data is not None else fixture(name), content_type='text/xml')


class ParserTests(SimpleTestCase):
    def read(self, name):
        return mspdi.read(io.BytesIO(fixture(name)), name)

    def test_reads_tasks_dates_milestones_summaries_and_resources(self):
        tasks, warnings = self.read('basic.xml')
        by = {t['uid']: t for t in tasks}
        self.assertEqual(list(by), [1, 2, 3, 4, 8])  # project summary, blank, inactive and undated left out
        self.assertTrue(by[1]['summary'])
        self.assertEqual(by[2]['name'], 'Strip out & soft demolition')
        self.assertEqual((by[2]['start'], by[2]['finish'], by[2]['progress']), ('2026-11-02', '2026-11-06', 100))
        self.assertEqual(by[3]['name'], 'Partitions and ceilings')  # whitespace tidied
        self.assertEqual((by[3]['after_uid'], by[3]['who'], by[3]['wbs']), (2, 'Pennine Drylining', '1.2'))
        self.assertTrue(by[4]['milestone'])
        self.assertEqual(by[4]['start'], by[4]['finish'])
        self.assertEqual(by[8]['finish'], '2026-11-13')  # midnight finish belongs to the day before
        self.assertIsNone(by[8]['after_uid'])  # predecessor not in the file
        self.assertEqual(warnings, ['1 task with no start date was left out.', '1 inactive task was left out.'])

    def test_no_namespace_date_only_backwards_and_duplicates(self):
        tasks, warnings = self.read('asta_no_namespace.xml')
        self.assertEqual([t['uid'] for t in tasks], [101, 102])
        self.assertTrue(tasks[0]['milestone'])
        self.assertEqual((tasks[1]['start'], tasks[1]['finish'], tasks[1]['progress']), ('2026-11-10', '2026-11-10', 100))
        self.assertEqual(len(warnings), 2)

    def test_refuses_what_is_not_mspdi_in_plain_english(self):
        cases = {
            'not_mspdi.xml': "isn't a Microsoft Project programme",
            'truncated.xml': 'cut short',
            'no_tasks.xml': 'no tasks',
            'entities.xml': "features we don't accept",
            'external_entity.xml': "features we don't accept",
        }
        for name, words in cases.items():
            with self.subTest(name), self.assertRaisesMessage(mspdi.NotMspdi, words):
                self.read(name)

    def test_refuses_mpp_excel_pdf_and_empty_files(self):
        for data, name, words in [
            (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\0' * 50, 'plan.mpp', '.mpp file'),
            (b'PK\x03\x04' + b'\0' * 50, 'plan.xlsx', "isn't a Microsoft Project XML file"),
            (b'%PDF-1.7 ...', 'plan.pdf', "isn't a Microsoft Project XML file"),
            (b'Task,Start,Finish\n', 'plan.txt', "isn't an XML file"),
            (b'   ', 'plan.xml', 'empty'),
        ]:
            with self.subTest(name), self.assertRaisesMessage(mspdi.NotMspdi, words):
                mspdi.read(io.BytesIO(data), name)

    def test_size_and_task_caps(self):
        with self.assertRaisesMessage(mspdi.NotMspdi, 'over 5 MB'):
            mspdi.read(io.BytesIO(b'<' + b' ' * mspdi.MAX_BYTES), 'big.xml')
        task = '<Task><UID>{}</UID><Name>T</Name><Start>2026-11-02T08:00:00</Start></Task>'
        many = ('<Project xmlns="http://schemas.microsoft.com/project"><Tasks>'
                + ''.join(task.format(i) for i in range(1, mspdi.MAX_TASKS + 3)) + '</Tasks></Project>').encode()
        with self.assertRaisesMessage(mspdi.NotMspdi, f'more than {mspdi.MAX_TASKS} tasks'):
            mspdi.read(io.BytesIO(many), 'many.xml')

    def test_finish_date_rule(self):
        from datetime import datetime as dt
        table = [  # start, finish written in the file, finish date kept
            (dt(2026, 11, 2, 8), dt(2026, 11, 6, 17), date(2026, 11, 6)),
            (dt(2026, 11, 2, 8), dt(2026, 11, 7, 0), date(2026, 11, 6)),
            (dt(2026, 11, 2, 0), dt(2026, 11, 2, 0), date(2026, 11, 2)),
        ]
        for start, finish, want in table:
            self.assertEqual(mspdi.finish_date(start, finish), want)


class ImportBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.joinery, _ = Trade.objects.get_or_create(name='Joinery')
            Package.objects.create(project=self.pa, subcontractor=self.subco, trade=self.joinery,
                                   status=Package.APPOINTED)
            self.hand = Activity.objects.create(project=self.pa, name='Hand-added survey', start=date(2026, 11, 3),
                                                finish=date(2026, 11, 3))

    def post_file(self, name, user=None, **extra):
        self.login(user or self.pat)
        return self.client.post(self.url('programme_import', self.pa.pk), {'file': upload(name, **extra)})

    def apply(self, imp, remove=False):
        data = {'action': 'apply'}
        if remove:
            data['remove_missing'] = '1'
        return self.client.post(self.url('programme_import_preview', self.pa.pk, imp.pk), data)

    def lines(self):
        with org_context(self.org_a):
            return {a.source_uid: a for a in Activity.objects.filter(project=self.pa).select_related('after')}


class ImportFlowTests(ImportBase):
    def test_upload_previews_then_apply_creates_lines(self):
        r = self.post_file('basic.xml')
        with org_context(self.org_a):
            imp = ProgrammeImport.objects.get()
        self.assertRedirects(r, self.url('programme_import_preview', self.pa.pk, imp.pk))
        self.assertEqual(self.lines().keys(), {None})  # nothing changes before you confirm
        page = self.client.get(r.url)
        self.assertContains(page, 'Check before importing')
        self.assertContains(page, 'Partitions and ceilings')
        self.assertContains(page, '1 task with no start date was left out.')
        r = self.apply(imp)
        self.assertRedirects(r, self.url('programme', self.pa.pk))
        lines = self.lines()
        self.assertEqual(set(lines), {None, 1, 2, 3, 4, 8})
        self.assertTrue(lines[1].is_summary)
        self.assertEqual(lines[3].after, lines[2])
        self.assertEqual(lines[8].subcontractor, self.subco)  # resource name matches an appointed sub
        self.assertEqual(lines[8].trade, self.joinery)
        self.assertEqual(lines[8].responsible, 'Sid Joinery')
        with org_context(self.org_a):
            imp.refresh_from_db()
        self.assertEqual((imp.status, imp.result), ('APPLIED', {'created': 5, 'updated': 0, 'removed': 0}))
        self.assertContains(self.client.get(self.url('programme', self.pa.pk)), 'Partitions and ceilings')

    def test_reimport_updates_by_uid_without_duplicates_and_keeps_local_settings(self):
        self.post_file('basic.xml')
        with org_context(self.org_a):
            self.apply(ProgrammeImport.objects.get())
            part = Activity.objects.get(project=self.pa, source_uid=3)
            part.is_shared = False  # set in Siteline: must survive a re-import
            part.save()
        self.post_file('updated.xml')
        with org_context(self.org_a):
            imp = ProgrammeImport.objects.filter(status='PREVIEW').get()
            plan = programme_import.compare(imp)
        self.assertEqual([t['uid'] for t in plan['new']], [9])
        self.assertEqual({a.source_uid: d for a, _, d in plan['changed']},
                         {3: ['start', 'finish', '% done'], 4: ['name', 'WBS', 'follows']})
        self.assertEqual(plan['same'], 2)
        self.assertEqual([a.source_uid for a in plan['missing']], [8])
        page = self.client.get(self.url('programme_import_preview', self.pa.pk, imp.pk))
        self.assertContains(page, 'Remove them from the programme')
        self.apply(imp)
        lines = self.lines()
        self.assertEqual(set(lines), {None, 1, 2, 3, 4, 8, 9})  # 8 kept: removal not ticked
        self.assertEqual((lines[3].start, lines[3].finish, lines[3].progress), (date(2026, 11, 11), date(2026, 11, 25), 50))
        self.assertFalse(lines[3].is_shared)
        self.assertEqual(lines[4].name, 'Practical completion (sectional)')
        self.assertEqual(lines[4].after, lines[9])
        self.assertEqual(lines[9].after, lines[3])
        with org_context(self.org_a):
            imp.refresh_from_db()
            self.assertEqual(imp.result, {'created': 1, 'updated': 2, 'removed': 0})
            self.assertEqual(Activity.objects.filter(project=self.pa).count(), 7)

    def test_remove_missing_only_when_ticked_and_never_hand_lines(self):
        self.post_file('basic.xml')
        with org_context(self.org_a):
            self.apply(ProgrammeImport.objects.get())
        self.post_file('updated.xml')
        with org_context(self.org_a):
            self.apply(ProgrammeImport.objects.get(status='PREVIEW'), remove=True)
        lines = self.lines()
        self.assertNotIn(8, lines)
        self.assertIn(None, lines)  # the hand-added survey stays
        self.assertEqual(lines[None].pk, self.hand.pk)

    def test_same_file_twice_changes_nothing(self):
        for _ in range(2):
            self.post_file('basic.xml')
            with org_context(self.org_a):
                self.apply(ProgrammeImport.objects.get(status='PREVIEW'))
        with org_context(self.org_a):
            last = ProgrammeImport.objects.order_by('-pk').first()
            self.assertEqual(last.result, {'created': 0, 'updated': 0, 'removed': 0})
            self.assertEqual(Activity.objects.filter(project=self.pa).count(), 6)

    def test_discard_and_no_double_apply(self):
        self.post_file('basic.xml')
        with org_context(self.org_a):
            imp = ProgrammeImport.objects.get()
        r = self.client.post(self.url('programme_import_preview', self.pa.pk, imp.pk), {'action': 'discard'})
        self.assertRedirects(r, self.url('programme_import', self.pa.pk))
        r = self.apply(imp)
        self.assertRedirects(r, self.url('programme_import', self.pa.pk))
        self.assertEqual(set(self.lines()), {None})
        page = self.client.get(self.url('programme_import_preview', self.pa.pk, imp.pk))
        self.assertContains(page, 'This file was discarded')

    def test_bad_files_show_the_reason_on_the_form(self):
        for name, words in [('not_mspdi.xml', 'isn&#x27;t a Microsoft Project programme'),
                            ('entities.xml', 'a DTD or entities'),
                            ('truncated.xml', 'cut short')]:
            r = self.post_file(name)
            self.assertEqual(r.status_code, 200, name)
            self.assertContains(r, words)
        r = self.post_file('basic.xml', data=b'\xd0\xcf\x11\xe0' + b'\0' * 40, as_name='plan.mpp')
        self.assertContains(r, 'Save it as XML first')
        with org_context(self.org_a):
            self.assertFalse(ProgrammeImport.objects.exists())

    @override_settings(MAX_UPLOAD_MB=0)
    def test_upload_validators_apply(self):
        r = self.post_file('basic.xml')
        self.assertContains(r, 'Files must be under 0 MB')

    def test_blocked_extension_refused(self):
        r = self.post_file('basic.xml', as_name='programme.html')
        self.assertContains(r, '.html files are not allowed')


class ImportAccessTests(ImportBase):
    def test_who_may_import(self):
        page = self.url('programme_import', self.pa.pk)
        for name, want in [('alice', 200), ('adam', 200), ('pat', 200), ('eve', 404), ('carla', 403), ('connie', 403),
                           ('sid', 403), ('bea', 403), ('nora', 404), ('bob', 404)]:
            with self.subTest(name):
                self.assertEqual(self.status_for(getattr(self, name), page), want)
        self.assertEqual(self.status_for(None, page), 302)

    def test_externals_cannot_post_or_preview(self):
        self.post_file('basic.xml')
        with org_context(self.org_a):
            imp = ProgrammeImport.objects.get()
        for u in (self.carla, self.sid):
            self.assertEqual(self.status_for(u, self.url('programme_import_preview', self.pa.pk, imp.pk)), 403)
            self.assertEqual(self.status_for(u, self.url('programme_import', self.pa.pk), 'post',
                                             {'file': upload('basic.xml')}), 403)

    def test_another_company_gets_404_and_cannot_reach_an_import(self):
        self.post_file('basic.xml')
        with org_context(self.org_a):
            imp = ProgrammeImport.objects.get()
        self.login(self.bob)
        self.assertEqual(self.client.get(self.url('programme_import_preview', self.pa.pk, imp.pk)).status_code, 404)
        # Bob's own project, Alpha's import id
        self.assertEqual(self.client.get(self.url('programme_import_preview', self.pb.pk, imp.pk, org=self.org_b))
                         .status_code, 404)
        self.assertEqual(self.client.post(self.url('programme_import_preview', self.pb.pk, imp.pk, org=self.org_b),
                                          {'action': 'apply'}).status_code, 404)

    def test_no_organisation_fails_closed(self):
        with self.assertRaises(TenantContextMissing):
            list(ProgrammeImport.objects.all())
        with self.assertRaises(TenantContextMissing):
            list(Activity.objects.filter(source_uid__isnull=False))

    def test_archived_job_is_read_only(self):
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.url('programme_import', self.pa.pk)).status_code, 200)
        r = self.client.post(self.url('programme_import', self.pa.pk), {'file': upload('basic.xml')})
        self.assertEqual(r.status_code, 403)

    def test_activity_form_offers_appointed_subs_and_saves_them(self):
        self.login(self.pat)
        r = self.client.post(self.url('programme', self.pa.pk), {
            'name': 'Bar back', 'start': '2026-11-09', 'finish': '2026-11-13', 'progress': '0',
            'subcontractor': self.subco.pk, 'trade': self.joinery.pk, 'is_shared': 'on'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            a = Activity.objects.get(name='Bar back')
        self.assertEqual((a.subcontractor, a.trade, a.who), (self.subco, self.joinery, 'Sid Joinery'))
        with org_context(self.org_b):
            from directory.models import Subcontractor
            other = Subcontractor.objects.create(company_name='Bravo Sub')
        r = self.client.post(self.url('programme', self.pa.pk), {
            'name': 'Sneaky', 'start': '2026-11-09', 'progress': '0', 'subcontractor': other.pk})
        self.assertEqual(r.status_code, 200)  # not a choice: another company's subcontractor
