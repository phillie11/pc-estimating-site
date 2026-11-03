"""
Review repros for the MS Project import and the look-ahead (branch claude/r3-programme-review).
Each test failed on origin/claude/r3-programme before its fix.
"""
import io
from datetime import date, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.utils import timezone

from directory.models import Subcontractor
from projects import mspdi, programme_import
from projects.models import Activity, ProgrammeImport
from tenancy.context import org_context

from .test_import import ImportBase

NS = 'http://schemas.microsoft.com/project'


def project_xml(tasks_xml):
    return (f'<?xml version="1.0"?><Project xmlns="{NS}"><Tasks>{tasks_xml}</Tasks></Project>').encode()


def task(uid, start='2026-11-02T08:00:00', finish='2026-11-06T17:00:00', name=None, **fields):
    extra = ''.join(f'<{k}>{v}</{k}>' for k, v in fields.items())
    return (f'<Task><UID>{uid}</UID><Name>{name or f"Task {uid}"}</Name><Start>{start}</Start>'
            f'<Finish>{finish}</Finish>{extra}</Task>')


def read(data):
    return mspdi.read(io.BytesIO(data), 'plan.xml')


class ParserRobustnessRepro(SimpleTestCase):
    def test_overflowing_numbers_do_not_crash_the_parser(self):
        # float('1e999') is inf, and int(inf) raises OverflowError, which _int didn't catch: a 500
        for field in ('PercentComplete', 'OutlineLevel'):
            with self.subTest(field):
                tasks, _ = read(project_xml(task(1, **{field: '1e999'}) + task(2)))
                self.assertEqual([t['uid'] for t in tasks], [1, 2])
        tasks, _ = read(project_xml(task('1e999') + task(2)))
        self.assertEqual([t['uid'] for t in tasks], [2])

    def test_task_ids_too_big_for_the_database_are_left_out(self):
        # source_uid is a PositiveIntegerField: 2**31 and up can't be stored on PostgreSQL or MySQL,
        # and 10**30 can't even be bound on SQLite. They must be refused at read time, not at apply.
        tasks, warnings = read(project_xml(task(2 ** 31) + task(10 ** 30) + task(5)))
        self.assertEqual([t['uid'] for t in tasks], [5])
        self.assertTrue(any('ID' in w for w in warnings), warnings)

    def test_dates_outside_what_ms_project_allows_are_left_out(self):
        # MS Project's own calendar runs 1984 to 2149. A year 9999 finish makes the gantt overflow
        # (it rounds the end up to a Sunday) and every programme page then fails.
        tasks, warnings = read(project_xml(
            task(1, start='9999-12-31T08:00:00', finish='9999-12-31T17:00:00')
            + task(2, start='0001-01-01T08:00:00', finish='0001-01-02T17:00:00')
            + task(3, finish='9999-12-31T17:00:00')
            + task(4)))
        self.assertEqual([t['uid'] for t in tasks], [3, 4])
        self.assertLessEqual(mspdi.as_date(tasks[0]['finish']), date(2149, 12, 31))
        self.assertTrue(warnings)

    def test_warnings_are_bounded(self):
        # Thousands of duplicate IDs used to give thousands of warnings, stored and shown on the preview
        many = project_xml(task(1) * 2000)
        tasks, warnings = read(many)
        self.assertEqual(len(tasks), 1)
        self.assertLessEqual(len(warnings), 10, warnings[:3])
        self.assertTrue(any('1999' in w for w in warnings), warnings[:3])

    def test_task_cap_counts_every_task_row_in_the_file(self):
        # The 3,000 cap only counted dated tasks it kept, after building the whole tree: a 5 MB file of
        # ~750,000 empty <Task/> rows was parsed in full (~70 MB, seconds of CPU) before being refused.
        many = project_xml('<Task/>' * (mspdi.MAX_TASKS + 10))
        with self.assertRaisesMessage(mspdi.NotMspdi, f'more than {mspdi.MAX_TASKS} tasks'):
            read(many)

    def test_task_cap_stops_reading_early(self):
        # Streaming: the parser stops at the cap rather than reading to the end. Anything broken after
        # the cap is never reached, so the refusal is about the size, not the broken XML.
        many = project_xml('<Task/>' * (mspdi.MAX_TASKS + 10))[:-len('</Tasks></Project>')] + b'<broken'
        with self.assertRaisesMessage(mspdi.NotMspdi, f'more than {mspdi.MAX_TASKS} tasks'):
            read(many)

    def test_dtd_and_entities_still_refused_when_streaming(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE Project [<!ENTITY a "aaaaaaaaaa">'
                b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                b'<Project xmlns="http://schemas.microsoft.com/project"><Tasks><Task><UID>1</UID><Name>&b;</Name>'
                b'<Start>2026-11-02</Start></Task></Tasks></Project>')
        with self.assertRaisesMessage(mspdi.NotMspdi, "features we don't accept"):
            read(bomb)
        external = (b'<?xml version="1.0"?><!DOCTYPE Project [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                    b'<Project><Tasks><Task><UID>1</UID><Name>&x;</Name><Start>2026-11-02</Start></Task></Tasks>'
                    b'</Project>')
        with self.assertRaisesMessage(mspdi.NotMspdi, "features we don't accept"):
            read(external)


class ImportRepro(ImportBase):
    def post_bytes(self, data, user=None):
        self.login(user or self.pat)
        return self.client.post(self.url('programme_import', self.pa.pk),
                                {'file': SimpleUploadedFile('plan.xml', data, content_type='text/xml')})

    def test_far_future_dates_cannot_break_the_programme_page(self):
        self.post_bytes(project_xml(task(1, finish='9999-12-31T17:00:00') + task(2)))
        with org_context(self.org_a):
            imp = ProgrammeImport.objects.get()
        self.apply(imp)
        self.assertEqual(self.client.get(self.url('programme', self.pa.pk)).status_code, 200)

    def test_an_older_preview_cannot_undo_a_newer_import(self):
        # Two people upload different versions; the newer is applied first. Applying the older preview
        # afterwards silently put the old dates back (and with "remove" ticked, deleted the new lines).
        self.post_bytes(project_xml(task(1, finish='2026-11-06T17:00:00')))
        with org_context(self.org_a):
            old = ProgrammeImport.objects.get()
        self.post_bytes(project_xml(task(1, finish='2026-11-20T17:00:00') + task(2)))
        with org_context(self.org_a):
            new = ProgrammeImport.objects.exclude(pk=old.pk).get()
        self.apply(new)
        r = self.apply(old, remove=True)
        self.assertRedirects(r, self.url('programme_import', self.pa.pk))
        lines = self.lines()
        self.assertEqual(lines[1].finish, date(2026, 11, 20))
        self.assertIn(2, lines)
        with org_context(self.org_a):
            old.refresh_from_db()
        self.assertNotEqual(old.status, ProgrammeImport.APPLIED)

    def test_apply_refuses_a_newer_import_overtaken_service_level(self):
        self.post_bytes(project_xml(task(1)))
        self.post_bytes(project_xml(task(1, name='Renamed')))
        with org_context(self.org_a):
            old, new = ProgrammeImport.objects.order_by('pk')
            programme_import.apply(new, self.pat)
            with self.assertRaises(ValueError):
                programme_import.apply(old, self.pat)


class ProgrammeChartLeakRepro(ImportBase):
    def test_subcontractor_does_not_see_other_subcontractors_on_the_chart(self):
        # The look-ahead hides "who" from a subcontractor; the chart showed it on every shared line,
        # and the import fills "who" from MS Project resource names (usually the subcontractors).
        today = timezone.localdate()
        with org_context(self.org_a):
            other = Subcontractor.objects.create(company_name='Pennine Drylining')
            Activity.objects.create(project=self.pa, name='Partitions L1', start=today,
                                    finish=today + timedelta(days=3), subcontractor=other)
            Activity.objects.create(project=self.pa, name='M&E first fix', start=today,
                                    finish=today + timedelta(days=3), responsible='Northern Sparks Ltd')
            Activity.objects.create(project=self.pa, name='Bar back', start=today, finish=today + timedelta(days=3),
                                    subcontractor=self.subco)
        self.login(self.sid)
        r = self.client.get(self.url('programme', self.pa.pk))
        self.assertContains(r, 'Partitions L1')  # the shared programme is still shown
        self.assertNotContains(r, 'Pennine Drylining')
        self.assertNotContains(r, 'Northern Sparks Ltd')
        self.assertContains(r, 'Sid Joinery')  # their own line still says it's theirs
        # Staff and the client still see who is doing what
        self.client.logout()
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('programme', self.pa.pk)), 'Pennine Drylining')
