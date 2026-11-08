"""The tender reader: which files are sent, the limits, the saved brief, and accepting a value."""
import base64
from datetime import date

from django.core.files.base import ContentFile
from django.test import override_settings

from ai import brief as briefs
from ai.models import AIRun, BidBrief, BriefItem
from projects.models import Document, DocumentRevision, FileScan, Project
from tenancy.context import org_context

from .base import Base, fake_ai, message, run_worker


def pdf(pages=2, marker=b''):
    body = b''.join(b'%d 0 obj << /Type /Page >> endobj\n' % (i + 3) for i in range(pages))
    return b'%PDF-1.4\n' + body + marker + b'\n%%EOF\n'


def brief_reply(**kw):
    item = {'section': 'risk', 'label': 'Uncapped LADs', 'text': 'LADs of £5,000 a week with no cap.', 'date': '',
            'date_kind': '', 'severity': 'critical', 'value_low': 0, 'value_high': 0, 'doc': 1, 'page': 2}
    data = {'recommendation': 'BID_WITH_CAUTION', 'summary': 'Office fit-out, JCT, 12 weeks.',
            'value_basis': 'Desktop, no site visit.',
            'items': [item,
                      {**item, 'section': 'date', 'label': 'Tender return', 'text': 'Return by noon.',
                       'date': '2027-01-15', 'date_kind': 'return', 'severity': 'high', 'doc': 2, 'page': 1},
                      {**item, 'section': 'scope', 'label': 'Partitions', 'text': 'Metal stud partitions.',
                       'value_low': 100000, 'value_high': 140000, 'severity': '', 'doc': 9, 'page': 400},
                      {**item, 'section': 'nonsense', 'text': 'dropped'},
                      {**item, 'section': 'risk', 'text': '<b>bold</b> ' + 'y' * 5000, 'severity': 'odd'}]}
    data.update(kw)
    return message(data)


class Setup(Base):
    def setUp(self):
        super().setUp()
        self.switch_on()
        with org_context(self.org_a):
            self.spec = self.doc(self.bid, 'KW-SPEC', 'Specification', pdf(3, b'SPEC-MARKER'))
            self.prelims = self.doc(self.bid, 'KW-PRE', 'Preliminaries', pdf(2, b'PRELIMS-MARKER'))
            self.other = self.doc(self.pa, 'PA-1', 'Another job\'s drawing', pdf(1, b'OTHER-JOB'))
        with org_context(self.org_b):
            self.bravo = self.doc(self.pb, 'B-1', 'Bravo drawing', pdf(1, b'BRAVO-COMPANY'))

    def doc(self, project, ref, title, data, name=None):
        d = Document.objects.create(project=project, title=title, reference=ref, category='TENDER')
        rev = DocumentRevision(revision='T1', file=ContentFile(data, name=name or f'{ref}.pdf'),
                               original_name=name or f'{ref}.pdf', size=len(data), page_count=None)
        d.add_revision(rev)
        return DocumentRevision.objects.get(document=d)

    def read(self, ids, *replies, user=None):
        self.login(user or self.eve)
        with fake_ai(*replies) as client:
            r = self.client.post(self.url('ai_brief_read', self.bid.pk), {'revisions': ids})
            run_worker()
        return r, client


class ReadTests(Setup):
    def sent(self, client):
        docs = [b for b in client.requests[0]['messages'][0]['content'] if b['type'] == 'document']
        return b''.join(base64.b64decode(d['source']['data']) for d in docs)

    def test_reads_the_chosen_pdfs_and_saves_a_checked_brief(self):
        r, client = self.read([self.spec.pk, self.prelims.pk], brief_reply())
        self.assertRedirects(r, self.url('ai_brief', self.bid.pk))
        sent = self.sent(client)
        self.assertIn(b'SPEC-MARKER', sent)
        self.assertIn(b'PRELIMS-MARKER', sent)
        with org_context(self.org_a):
            brief = BidBrief.objects.get(project=self.bid)
            items = list(brief.items.all())
            run = AIRun.objects.get()
            self.bid.refresh_from_db()
        self.assertEqual(run.status, AIRun.DONE, run.message)
        self.assertEqual(brief.recommendation, BidBrief.CAUTION)
        self.assertEqual(len(items), 4)  # the unknown section is dropped
        risk = next(i for i in items if i.label == 'Uncapped LADs')
        self.assertEqual((risk.source_revision, risk.source_page, risk.severity), (self.spec, 2, 'critical'))
        scope = next(i for i in items if i.section == 'scope')
        self.assertIsNone(scope.source_revision)  # document 9 doesn't exist: no made-up source
        self.assertEqual(scope.value_low, 100000)
        long = next(i for i in items if i.text.startswith('<b>'))
        self.assertLessEqual(len(long.text), briefs.MAX_TEXT)
        self.assertEqual(long.severity, '')
        # Nothing reaches the project until accepted
        self.assertIsNone(self.bid.tender_due)
        page = self.client.get(self.url('ai_brief', self.bid.pk))
        self.assertContains(page, '&lt;b&gt;bold&lt;/b&gt;')
        self.assertContains(page, 'KW-SPEC Specification rev T1, page 2')
        date_item = next(i for i in items if i.section == 'date')
        self.client.post(self.url('ai_brief_accept', self.bid.pk, date_item.pk))
        with org_context(self.org_a):
            self.bid.refresh_from_db()
            date_item.refresh_from_db()
        self.assertEqual(self.bid.tender_return_date, date(2027, 1, 15))
        self.assertEqual(date_item.accepted_by, self.eve)

    def test_never_sends_another_projects_or_companys_files(self):
        # The page only offers this bid's files, so a posted foreign id fails the form and queues nothing
        r, client = self.read([self.spec.pk, self.other.pk, self.bravo.pk], brief_reply())
        self.assertEqual(client.requests, [])
        self.assertFalse(AIRun.all_objects.exists())
        # And the worker itself drops any id that isn't this bid's, whatever it is given
        with fake_ai(), org_context(self.org_a):
            docs, _ = briefs.gather(self.bid, [self.spec.pk, self.other.pk, self.bravo.pk], self.eve)
        self.assertEqual([d[1] for d in docs], [self.spec])

    def test_blocked_or_unscanned_files_are_never_read(self):
        with org_context(self.org_a):
            FileScan.all_objects.create(path=self.spec.file.name, status=FileScan.INFECTED, organization=self.org_a)
        from unittest import mock
        with mock.patch('projects.files.scanning_enabled', return_value=True), \
                mock.patch('projects.files.fetch_scan_tag', return_value=None):
            r, client = self.read([self.spec.pk, self.prelims.pk], brief_reply())
        self.assertEqual(client.requests, [])  # one blocked, one not scanned yet: nothing is sent
        with org_context(self.org_a):
            run = AIRun.objects.get()
        self.assertEqual(run.status, AIRun.FAILED)
        self.assertIn('blocked by the virus check', run.message)
        self.assertIn('hasn\'t finished', run.message)

    @override_settings(AI_TENDER_MAX_PAGES=4)
    def test_page_cap_leaves_files_out_with_a_clear_message(self):
        r, client = self.read([self.spec.pk, self.prelims.pk], brief_reply())
        sent = self.sent(client)
        self.assertIn(b'SPEC-MARKER', sent)
        self.assertNotIn(b'PRELIMS-MARKER', sent)
        with org_context(self.org_a):
            run = AIRun.objects.get()
        self.assertIn('limited to about 4 pages', run.message)

    def test_nothing_readable_sends_nothing(self):
        with org_context(self.org_a):
            fake = self.doc(self.bid, 'KW-X', 'Not really a PDF', b'MZ not a pdf', name='x.pdf')
        r, client = self.read([fake.pk])
        self.assertEqual(client.requests, [])
        with org_context(self.org_a):
            run = AIRun.objects.get()
        self.assertEqual(run.status, AIRun.FAILED)
        self.assertIn('not a readable PDF', run.message)

    def test_reading_again_keeps_edited_and_accepted_lines(self):
        self.read([self.spec.pk], brief_reply())
        with org_context(self.org_a):
            brief = BidBrief.objects.get(project=self.bid)
            keep = brief.items.get(text__startswith='LADs of')
        self.client.post(self.url('ai_brief_item', self.bid.pk, keep.pk),
                         {'section': 'risk', 'label': 'Uncapped LADs', 'text': 'Checked: cap is 10%.',
                          'severity': 'high'})
        self.read([self.spec.pk], brief_reply(items=[]))
        with org_context(self.org_a):
            self.assertEqual(list(brief.items.values_list('text', flat=True)), ['Checked: cap is 10%.'])


class ChoicesTests(Setup):
    def test_only_current_pdfs_on_this_bid_are_offered(self):
        with org_context(self.org_a):
            self.doc(self.bid, 'KW-DOCX', 'Word file', b'PK', name='notes.docx')
            offered = briefs.choices(self.bid)
        self.assertEqual({r.pk for r in offered}, {self.spec.pk, self.prelims.pk})
