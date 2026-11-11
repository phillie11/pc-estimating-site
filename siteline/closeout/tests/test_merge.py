"""
The O&M manual as one PDF (closeout/merge.py): who may ask for it and download it (the same people as the
zip), other companies, the cover and contents pages, bookmarks, non-PDF files listed as supplied separately,
and hostile or broken files (encrypted, unreadable, scripts and links, over the caps, not cleared by the
virus check) that are skipped and listed without stopping the manual.
"""
import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from pypdf import PdfReader
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from documents.tests.test_pdftext import hostile_pdf
from jobs.models import Job
from tenancy.context import org_context
from tenancy.models import Notification

from .. import merge
from ..models import OMCompilation, OMSection
from ..tasks import merge_om_pdf
from .base import CloseoutTestBase


def real_pdf(*pages, encrypt=None):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, encrypt=StandardEncryption(encrypt) if encrypt else None)
    for words in pages:
        c.drawString(72, 700, words)
        c.showPage()
    c.save()
    return buf.getvalue()


def up(name, data):
    return SimpleUploadedFile(name, data, content_type='application/octet-stream')


class MergeBase(CloseoutTestBase):
    def setUp(self):
        super().setUp()
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'setup'})
        with org_context(self.org_a):
            self.sections = {s.title: s for s in OMSection.objects.filter(project=self.pa)}

    def add(self, section, title, name, data):
        r = self.client.post(self.url('closeout_om_upload', self.pa.pk, self.sections[section].pk),
                             {'title': title, 'file': up(name, data)})
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)

    def compile(self):
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'compile'})
        with org_context(self.org_a):
            return OMCompilation.objects.filter(project=self.pa).first()  # the newest: ordered by -number

    def merge(self, comp):
        """Ask for the one PDF as staff and run the job the way the worker would."""
        self.login(self.pat)
        r = self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'merge', 'compilation': comp.pk})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            for job in Job.objects.filter(name='closeout.merge_om_pdf'):
                merge_om_pdf(job.payload)
            return OMCompilation.objects.get(pk=comp.pk)

    def download(self, comp, user=None):
        if user is not None:
            self.login(user)
        r = self.client.get(self.url('closeout_om_download_pdf', self.pa.pk, comp.pk))
        return r, (b''.join(r.streaming_content) if r.status_code == 200 else b'')

    def full_manual(self):
        self.add('Health and safety file', 'Residual hazards', 'hs.pdf', real_pdf('Residual hazards text', 'Page two'))
        self.add('Joinery', 'Veneer data', 'veneer.pdf', real_pdf('Veneer data sheet'))
        self.add('Joinery', 'Cutting list', 'cutting.docx', b'PK\x03\x04 not really a document')
        self.add('Ceilings', 'Tile warranty', 'tile.pdf', b'%PDF-1.4 this one is broken')
        return self.compile()


class ContentsAndBookmarkTests(MergeBase):
    def test_one_pdf_has_cover_contents_bookmarks_and_lists_what_was_not_merged(self):
        comp = self.full_manual()
        self.assertEqual(comp.status, OMCompilation.READY)
        comp = self.merge(comp)
        self.assertEqual((comp.merged_status, comp.merged_error), (OMCompilation.MERGE_READY, ''))
        r, data = self.download(comp)
        self.assertEqual(r.status_code, 200)
        self.assertIn('OM-manual-v1.pdf', r['Content-Disposition'])
        self.assertTrue(r['Content-Disposition'].split('filename=')[1].strip('"').startswith(self.pa.reference))
        reader = PdfReader(io.BytesIO(data))
        # Cover, contents, then 2 + 1 merged pages
        self.assertEqual(len(reader.pages), 2 + 3)
        self.assertEqual(comp.merged_pages, 5)
        cover = reader.pages[0].extract_text()
        for word in ('Alpha Fitout', 'Operation and maintenance manual', self.pa.reference, 'Version', '1'):
            self.assertIn(word, cover)
        contents = reader.pages[1].extract_text()
        self.assertIn('Residual hazards', contents)
        self.assertIn('Supplied separately, see the zip', contents)
        self.assertIn('Not included: it could not be read', contents)
        self.assertEqual(comp.merged_skipped, [['Ceilings', 'Tile warranty', 'it could not be read']])

        # Bookmarks: cover, contents, then each section with its documents, in manual order and on the right page
        def walk(items, depth=0):
            for item in items:
                if isinstance(item, list):
                    yield from walk(item, depth + 1)
                else:
                    yield depth, item.title, reader.get_destination_page_number(item)
        marks = list(walk(reader.outline))
        self.assertEqual(marks[:2], [(0, 'Cover', 0), (0, 'Contents', 1)])
        by_title = {t: (d, p) for d, t, p in marks}
        hs_section = next(t for t in by_title if t.endswith('Health and safety file'))
        joinery_section = next(t for t in by_title if t.endswith('Joinery'))
        self.assertEqual(by_title['Residual hazards'], (1, 2))
        self.assertEqual(by_title['Veneer data'], (1, 4))
        self.assertEqual(by_title[hs_section], (0, 2))
        self.assertEqual(by_title[joinery_section], (0, 4))
        self.assertNotIn('Cutting list', by_title)  # not a PDF: not merged, so no bookmark
        self.assertNotIn('Tile warranty', by_title)
        # Manual order: the sections come in the order of the manual
        order = [t for d, t, _ in marks if d == 0][2:]
        self.assertEqual(order, sorted(order, key=lambda t: int(t[:2])))
        # The pages really are the files' pages
        self.assertIn('Residual hazards text', reader.pages[2].extract_text())
        self.assertIn('Page two', reader.pages[3].extract_text())
        self.assertIn('Veneer data sheet', reader.pages[4].extract_text())

    def test_the_requester_is_told_and_asking_twice_makes_one_job(self):
        comp = self.full_manual()
        self.login(self.pat)
        for _ in range(2):
            self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'merge', 'compilation': comp.pk})
        with org_context(self.org_a):
            self.assertEqual(Job.objects.filter(name='closeout.merge_om_pdf').count(), 1)
            self.assertEqual(OMCompilation.objects.get(pk=comp.pk).merged_status, OMCompilation.MERGE_QUEUED)
        self.assertContains(self.client.get(self.url('closeout_om', self.pa.pk)), 'One PDF is being put together')
        comp = self.merge(comp)
        with org_context(self.org_a):
            merge_om_pdf({'id': comp.pk})  # a second run changes nothing
            self.assertTrue(Notification.objects.filter(user=self.pat, title__contains='one PDF').exists())
        self.assertContains(self.client.get(self.url('closeout_om', self.pa.pk)), 'Download as one PDF')

    def test_the_page_offers_the_button_beside_the_zip(self):
        comp = self.full_manual()
        page = self.client.get(self.url('closeout_om', self.pa.pk))
        self.assertContains(page, 'Download as one PDF')
        self.assertContains(page, self.url('closeout_om_download', self.pa.pk, comp.pk))

    def test_files_left_out_of_the_zip_stay_out_and_are_listed(self):
        self.add('Joinery', 'Veneer data', 'veneer.pdf', real_pdf('Veneer data sheet'))
        with mock.patch('closeout.services.scan_status', return_value=None):
            comp = self.compile()
        self.assertEqual(comp.file_count, 0)
        comp = self.merge(comp)
        _, data = self.download(comp)
        reader = PdfReader(io.BytesIO(data))
        self.assertEqual(len(reader.pages), 2)
        text = reader.pages[1].extract_text()
        self.assertIn('Left out of the zip as well', text)
        self.assertIn('Waiting for virus check', text)
        self.assertNotIn('Veneer data sheet', ''.join(p.extract_text() for p in reader.pages))

    def test_a_manual_compiled_before_this_has_no_button_and_says_why(self):
        comp = self.full_manual()
        with org_context(self.org_a):
            OMCompilation.objects.filter(pk=comp.pk).update(manifest=[])
        self.assertNotContains(self.client.get(self.url('closeout_om', self.pa.pk)), 'Download as one PDF')
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'merge', 'compilation': comp.pk})
        with org_context(self.org_a):
            self.assertFalse(Job.objects.filter(name='closeout.merge_om_pdf').exists())
        self.assertContains(self.client.get(self.url('closeout_om', self.pa.pk)), 'Compile this manual again first')


class HostileFileTests(MergeBase):
    def merged(self):
        comp = self.compile()
        comp = self.merge(comp)
        _, data = self.download(comp)
        return comp, PdfReader(io.BytesIO(data))

    def test_encrypted_files_are_skipped_and_listed(self):
        self.add('Joinery', 'Locked data', 'locked.pdf', real_pdf('Secret', encrypt='pw'))
        self.add('Joinery', 'Open data', 'open.pdf', real_pdf('Open words'))
        comp, reader = self.merged()
        self.assertEqual(comp.merged_skipped, [['Joinery', 'Locked data', 'it is password protected']])
        self.assertEqual(len(reader.pages), 3)
        self.assertNotIn('Secret', ''.join(p.extract_text() for p in reader.pages))

    def test_links_scripts_attachments_and_open_actions_are_not_carried_over(self):
        self.add('Joinery', 'Active file', 'active.pdf', hostile_pdf())
        comp, reader = self.merged()
        self.assertEqual(len(reader.pages), 3)
        self.assertIn('Plain words only', reader.pages[2].extract_text())
        root = reader.trailer['/Root']
        for key in ('/OpenAction', '/Names', '/AA', '/AcroForm'):
            self.assertNotIn(key, root)
        for page in reader.pages:
            self.assertNotIn('/Annots', page)
            self.assertNotIn('/AA', page)
        raw = self.download(comp)[1]
        self.assertNotIn(b'JavaScript', raw)
        self.assertNotIn(b'127.0.0.1', raw)

    def test_files_over_the_caps_are_skipped_and_listed(self):
        self.add('Joinery', 'First', 'one.pdf', real_pdf('One', 'Two', 'Three'))
        self.add('Joinery', 'Second', 'two.pdf', real_pdf('Four'))
        with mock.patch.object(merge, 'MAX_FILE_PAGES', 2):
            comp, reader = self.merged()
        self.assertEqual(comp.merged_skipped, [['Joinery', 'First', 'it has more than 2 pages']])
        self.assertEqual(len(reader.pages), 3)
        self.add('Joinery', 'Third', 'three.pdf', real_pdf('Five'))
        comp = self.compile()
        with mock.patch.object(merge, 'MAX_TOTAL_PAGES', 1):
            comp = self.merge(comp)
        self.assertEqual([s[2] for s in comp.merged_skipped], ['the manual is already at its page limit'] * 2)
        comp = self.compile()
        with mock.patch.object(merge, 'MAX_FILE_MB', 0):
            comp = self.merge(comp)
        self.assertTrue(all(s[2] == 'it is over 0 MB' for s in comp.merged_skipped))

    def test_a_job_that_runs_out_of_time_lists_the_rest(self):
        self.add('Joinery', 'First', 'one.pdf', real_pdf('One'))
        comp = self.compile()
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'merge', 'compilation': comp.pk})
        with org_context(self.org_a):
            comp = OMCompilation.objects.get(pk=comp.pk)
            merge.merge_om(comp, now=iter([0] + [10 ** 9] * 100).__next__)
            comp.refresh_from_db()
        self.assertEqual(comp.merged_skipped, [['Joinery', 'First', 'there was not enough time to read it']])

    def test_a_manual_that_cannot_be_built_says_so_instead_of_staying_queued(self):
        comp = self.full_manual()
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'merge', 'compilation': comp.pk})
        with org_context(self.org_a):
            merge.gave_up(comp.pk)
            comp = OMCompilation.objects.get(pk=comp.pk)
        self.assertEqual(comp.merged_status, OMCompilation.MERGE_FAILED)
        page = self.client.get(self.url('closeout_om', self.pa.pk))
        self.assertContains(page, 'could not be put together as one PDF')
        self.assertContains(page, 'Download as one PDF')  # and it can be tried again
        self.assertEqual(self.download(comp)[0].status_code, 404)

    def test_text_typed_by_people_is_printed_not_obeyed(self):
        self.add('Joinery', '<b>Bold</b> <img src="file:///etc/passwd"> &amp; <font color=red>x', 'a.pdf', real_pdf('A'))
        comp, reader = self.merged()
        text = reader.pages[1].extract_text()
        self.assertIn('<b>Bold</b>', text)
        self.assertIn('<img src="file:///etc/passwd">', text)


class WhoMayDownloadTests(MergeBase):
    def setUp(self):
        super().setUp()
        self.add('Joinery', 'Veneer data', 'veneer.pdf', real_pdf('Veneer data sheet'))
        self.comp = self.merge(self.compile())

    def status(self, user, name='closeout_om_download_pdf', method='get', data=None, project=None, comp=None, org=None):
        project = project or self.pa
        url = self.url(name, project.pk, (comp or self.comp).pk, org=org) if org else self.url(
            name, project.pk, (comp or self.comp).pk)
        return self.status_for(user, url, method, data)

    def test_staff_get_it_whoever_asked(self):
        for user in (self.pat, self.alice, self.adam):
            self.assertEqual(self.status(user), 200, user.email)

    def test_the_client_side_only_once_it_is_shared_like_the_zip(self):
        zip_before = self.status(self.carla, 'closeout_om_download')
        self.assertEqual((self.status(self.carla), zip_before), (404, 404))
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'share', 'compilation': self.comp.pk})
        for user in (self.carla, self.connie):
            self.assertEqual(self.status(user), 200, user.email)
            self.assertEqual(self.status(user, 'closeout_om_download'), 200, user.email)
        self.assertContains(self.client.get(self.url('closeout_client', self.pa.pk)), 'Download as one PDF')
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'unshare', 'compilation': self.comp.pk})
        self.assertEqual(self.status(self.carla), 404)

    def test_subcontractors_and_people_outside_the_job_are_refused_like_the_zip(self):
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'share', 'compilation': self.comp.pk})
        self.assertEqual(self.status(self.sid), 403)
        self.assertEqual(self.status(None), 302)  # not signed in: sent to sign in
        for user in (self.sid, self.bea, self.nora, self.bob):
            self.assertEqual(self.status(user), self.status(user, 'closeout_om_download'), user.email)
            self.assertIn(self.status(user), (403, 404), user.email)

    def test_only_staff_can_ask_for_it(self):
        with org_context(self.org_a):
            OMCompilation.objects.filter(pk=self.comp.pk).update(merged_status='', merged_file='')
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'share', 'compilation': self.comp.pk})
        with org_context(self.org_a):
            jobs_before = Job.objects.filter(name='closeout.merge_om_pdf').count()
        for user in (self.carla, self.connie, self.sid, self.bea, self.nora):
            self.assertIn(self.status_for(user, self.url('closeout_om', self.pa.pk), 'post',
                                          {'action': 'merge', 'compilation': self.comp.pk}), (403, 404), user.email)
        with org_context(self.org_a):
            self.assertEqual(OMCompilation.objects.get(pk=self.comp.pk).merged_status, '')
            self.assertEqual(Job.objects.filter(name='closeout.merge_om_pdf').count(), jobs_before)

    def test_not_made_yet_is_a_404_for_everyone(self):
        with org_context(self.org_a):
            OMCompilation.objects.filter(pk=self.comp.pk).update(merged_status='')
        self.assertEqual(self.status(self.pat), 404)

    def test_another_company_cannot_reach_it_or_ask_for_it(self):
        # Bob is in the other company: the URL is under Alpha's workspace name
        self.assertEqual(self.status(self.bob), 404)
        self.assertEqual(self.status_for(self.bob, self.url('closeout_om', self.pa.pk), 'post',
                                         {'action': 'merge', 'compilation': self.comp.pk}), 404)
        # A member of both companies cannot reach Alpha's manual through Bravo's job, by id
        from tenancy.models import Membership
        with org_context(self.org_b):
            Membership.objects.create(organization=self.org_b, user=self.pat, role=Membership.ROLE_MEMBER)
        self.assertEqual(self.status_for(self.pat, self.url('closeout_om_download_pdf', self.pb.pk, self.comp.pk,
                                                            org=self.org_b)), 404)
        # And the job only finds manuals inside its own company
        with org_context(self.org_b):
            self.assertFalse(OMCompilation.objects.filter(pk=self.comp.pk).exists())
            merge_om_pdf({'id': self.comp.pk})  # finds nothing, does nothing

    def test_downloads_are_logged_like_the_zip(self):
        from projects.models import FileAccess
        self.login(self.pat)
        self.client.get(self.url('closeout_om_download_pdf', self.pa.pk, self.comp.pk))
        with org_context(self.org_a):
            self.assertTrue(FileAccess.objects.filter(label__contains='one PDF').exists())
