"""
AI quote mapping: what is sent (one quote, our scope lines, the typed total, nothing from other bidders), how the
answer is checked, that nothing reaches the levelling until accepted, the tie to the total, and who can see it.
"""
import io
import json
from decimal import Decimal as D

from django.core.files.base import ContentFile

from ai import quotemap
from ai.models import AIRun, AIUsage, QuoteMapDraft, QuoteMapExclusion, QuoteMapLine
from directory.models import Package, Subcontractor, Trade
from levelling import services as levelling
from levelling.models import Exclusion, QuoteCell
from projects.models import FileScan, Project
from tenancy.context import org_context

from .base import Base, fake_ai, message, run_worker
from .test_brief import pdf


def xlsx_bytes(rows, sheet='Quote'):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def answer(lines=None, **kw):
    data = {'quote_total_pence': 1_200_000, 'total_source': 'Page 1, total', 'ties_to_total': True,
            'difference_pence': 0, 'unmatched_quote_items': [], 'exclusions': [],
            'lines': lines if lines is not None else [
                {'scope_key': 'S1', 'rate_pence': 5000, 'amount_pence': -1, 'included_elsewhere': False,
                 'source': 'Page 1, item 1', 'note': ''},
                {'scope_key': 'S2', 'rate_pence': -1, 'amount_pence': 200000, 'included_elsewhere': False,
                 'source': 'Page 1, item 2', 'note': 'Lump sum'}]}
    data.update(kw)
    return message(data)


class Setup(Base):
    def setUp(self):
        super().setUp()
        self.switch_on()
        with org_context(self.org_a):
            self.trade = Trade.objects.create(name='Partitions')
            self.other_sub = Subcontractor.objects.create(company_name='Rival Partitions Ltd')
            # bea, the tendering subcontractor on the bid, is self.subco's person: their quote is the one mapped
            self.pkg = Package.objects.create(project=self.bid, subcontractor=self.subco, trade=self.trade,
                                              status=Package.QUOTED, quote_value=D('12000.00'))
            self.pkg.quote_file.save('quote-a.pdf', ContentFile(pdf(2, b'OUR-BIDDER-QUOTE')), save=True)
            self.rival = Package.objects.create(project=self.bid, subcontractor=self.other_sub, trade=self.trade,
                                                status=Package.QUOTED, quote_value=D('9876.54'))
            self.rival.quote_file.save('quote-b.pdf', ContentFile(pdf(1, b'RIVAL-QUOTE')), save=True)
            self.sheet = levelling.ensure_saved(levelling.sheet_for(self.bid, self.trade))
            self.l1 = levelling.add_line(self.sheet, 'P1', 'Metal stud partition, 2x15mm', D('200'), 'm2')
            self.l2 = levelling.add_line(self.sheet, 'P2', 'Bulkheads to perimeter', None, 'item')
            self.l3 = levelling.add_line(self.sheet, 'P3', 'Access hatches', D('12'), 'nr')
            levelling.set_cell(self.l1, self.rival, rate=D('47.77'))  # a rival price that must never be sent or shown

    def start(self, *replies, user=None, counts=None, package=None):
        self.login(user or self.eve)
        p = package or self.pkg
        with fake_ai(*replies, counts=counts) as client:
            r = self.client.post(self.url('ai_quote_map_start', self.bid.pk, self.trade.pk, p.pk))
            run_worker()
        return r, client

    def draft(self):
        with org_context(self.org_a):
            return QuoteMapDraft.objects.filter(package=self.pkg).first()


class InputTests(Setup):
    def test_pdf_quote_our_lines_and_typed_total_only(self):
        r, client = self.start(answer(), counts=[3000])
        self.assertRedirects(r, self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk))
        content = client.requests[0]['messages'][0]['content']
        import base64
        docs = [base64.b64decode(b['source']['data']) for b in content if b['type'] == 'document']
        text = ' '.join(b['text'] for b in content if b['type'] == 'text')
        self.assertEqual(len(docs), 1)
        self.assertIn(b'OUR-BIDDER-QUOTE', docs[0])
        self.assertIn('S1 | P1 | Metal stud partition, 2x15mm | 200 | m2', text)
        self.assertIn('S3 | P3 | Access hatches | 12 | nr', text)
        self.assertIn('£12,000.00', text)
        for leak in ('RIVAL', 'Rival', '47.77', '9,876.54', 'Eve'):
            self.assertNotIn(leak, text)
        self.assertNotIn(b'RIVAL-QUOTE', b''.join(docs))
        self.assertEqual(len(client.count_requests), 1)  # the PDF was counted before the hold
        with org_context(self.org_a):
            u = AIUsage.objects.get()
        self.assertEqual(u.feature, 'quote_mapping')

    def test_a_workbook_is_cleaned_and_sent_as_cell_referenced_text(self):
        with org_context(self.org_a):
            self.pkg.quote_file.save('quote.xlsx', ContentFile(xlsx_bytes([
                ['Item', 'Description', 'Rate'], ['1', 'Stud partition', 52.5], ['', 'Total', 12000]])), save=True)
        r, client = self.start(answer())
        content = client.requests[0]['messages'][0]['content']
        self.assertFalse([b for b in content if b['type'] == 'document'])
        text = content[0]['text']
        self.assertIn('Quote!B2: Stud partition', text)
        self.assertIn('Quote!C2: 52.5', text)
        self.assertIn('Quote!C3: 12000', text)

    def test_a_workbook_with_a_macro_sheet_is_refused_and_nothing_sent(self):
        with org_context(self.org_a):
            self.pkg.quote_file.save('quote.xlsx', ContentFile(b'PK\x03\x04 not really a workbook'), save=True)
        r, client = self.start(answer())
        self.assertEqual(client.requests, [])
        with org_context(self.org_a):
            run = AIRun.objects.get()
            self.assertFalse(AIUsage.objects.exists())
        self.assertEqual(run.status, AIRun.FAILED)

    def test_unsupported_types_and_unscanned_files_send_nothing(self):
        with org_context(self.org_a):
            self.pkg.quote_file.save('quote.docx', ContentFile(b'PK'), save=True)
        r, client = self.start(answer())
        self.assertEqual(client.requests, [])
        with org_context(self.org_a):
            self.pkg.quote_file.save('quote.pdf', ContentFile(pdf(1)), save=True)
            FileScan.all_objects.create(path=self.pkg.quote_file.name, status=FileScan.INFECTED,
                                        organization=self.org_a)
        from unittest import mock
        with mock.patch('projects.files.scanning_enabled', return_value=True):
            r, client = self.start(answer())
        self.assertEqual(client.requests, [])
        with org_context(self.org_a):
            self.assertIn('virus check', AIRun.objects.first().message)


class SaveTests(Setup):
    def test_only_sent_keys_sane_amounts_and_capped_text(self):
        lines = [
            {'scope_key': 'S1', 'rate_pence': 5000, 'amount_pence': -1, 'included_elsewhere': False,
             'source': 'p1', 'note': '<script>x</script>' + 'n' * 900},
            {'scope_key': 'S1', 'rate_pence': 1, 'amount_pence': -1, 'included_elsewhere': False, 'source': '',
             'note': 'duplicate'},
            {'scope_key': 'S99', 'rate_pence': 100, 'amount_pence': -1, 'included_elsewhere': False, 'source': '',
             'note': 'unknown key'},
            {'scope_key': 'S2', 'rate_pence': -500, 'amount_pence': 10 ** 15, 'included_elsewhere': False,
             'source': '', 'note': 'nonsense money'},
            {'scope_key': 'S3', 'rate_pence': -1, 'amount_pence': -1, 'included_elsewhere': True, 'source': 'p2',
             'note': 'in item 1'},
        ]
        excl = [{'text': 'Working hours 08:00 to 17:00 only.', 'source': 'p3'}, {'text': 'x' * 900, 'source': ''}]
        self.start(answer(lines, exclusions=excl, unmatched_quote_items=[
            {'text': 'Optional fire upgrade', 'amount_pence': 45000, 'source': 'p2'},
            {'text': 'Silly', 'amount_pence': 10 ** 16, 'source': ''}]))
        d = self.draft()
        with org_context(self.org_a):
            rows = {m.line_id: m for m in d.lines.all()}
            excs = list(d.exclusions.values_list('text', flat=True))
        self.assertEqual(set(rows), {self.l1.pk, self.l3.pk})
        self.assertEqual(rows[self.l1.pk].rate, D('50.00'))
        self.assertLessEqual(len(rows[self.l1.pk].note), 500)
        self.assertTrue(rows[self.l3.pk].included)
        self.assertEqual(excs[0], 'Working hours 08:00 to 17:00 only.')
        self.assertEqual(len(excs[1]), 500)
        self.assertEqual(d.unmatched[0]['amount'], '450.00')
        self.assertEqual(d.unmatched[1]['amount'], '')
        page = self.client.get(self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk))
        self.assertContains(page, '&lt;script&gt;')
        self.assertNotContains(page, '<script>x')


class AcceptTests(Setup):
    def test_nothing_reaches_the_levelling_until_accepted(self):
        self.start(answer(exclusions=[{'text': 'Excludes out of hours working.', 'source': 'p2'}]))
        d = self.draft()
        with org_context(self.org_a):
            self.assertFalse(QuoteCell.objects.filter(package=self.pkg).exists())
            self.assertFalse(Exclusion.objects.filter(package=self.pkg).exists())
            m1 = d.lines.get(line=self.l1)
            x = d.exclusions.get()
        url = self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk)
        self.client.post(url, {'action': 'accept_line', 'item': m1.pk})
        self.client.post(url, {'action': 'accept_exclusion', 'item': x.pk})
        with org_context(self.org_a):
            cell = QuoteCell.objects.get(package=self.pkg, line=self.l1)
            self.assertEqual(cell.rate, D('50.00'))
            self.assertFalse(QuoteCell.objects.filter(package=self.pkg, line=self.l2).exists())
            self.assertEqual(Exclusion.objects.get(package=self.pkg).text, 'Excludes out of hours working.')
            self.assertEqual(Exclusion.objects.get(package=self.pkg).status, Exclusion.OPEN)
        self.client.post(url, {'action': 'accept_all'})
        with org_context(self.org_a):
            self.assertEqual(QuoteCell.objects.get(package=self.pkg, line=self.l2).amount, D('2000.00'))
            self.assertEqual(Exclusion.objects.filter(package=self.pkg).count(), 1)  # no duplicate
            d.refresh_from_db()
            self.assertEqual(d.status, QuoteMapDraft.DONE)
            # The rival's price is untouched
            self.assertEqual(QuoteCell.objects.get(package=self.rival, line=self.l1).rate, D('47.77'))
        self.assertEqual(self.client.post(url, {'action': 'accept_all'}).status_code, 302)  # already reviewed

    def test_reject_and_discard(self):
        self.start(answer())
        d = self.draft()
        url = self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk)
        with org_context(self.org_a):
            m1 = d.lines.get(line=self.l1)
        self.client.post(url, {'action': 'reject_line', 'item': m1.pk})
        self.client.post(url, {'action': 'discard'})
        with org_context(self.org_a):
            self.assertFalse(QuoteCell.objects.filter(package=self.pkg).exists())
            m1.refresh_from_db()
            d.refresh_from_db()
        self.assertEqual((m1.status, d.status), (QuoteMapLine.REJECTED, QuoteMapDraft.DISCARDED))

    def test_mapping_again_replaces_the_open_draft(self):
        self.start(answer())
        self.start(answer())
        with org_context(self.org_a):
            self.assertEqual(QuoteMapDraft.objects.filter(package=self.pkg, status=QuoteMapDraft.OPEN).count(), 1)


class TieTests(Setup):
    def test_siteline_works_out_the_tie_itself(self):
        # S1 50.00 x 200 = 10,000; S2 2,000 sum: 12,000 against a 12,000 total, whatever the model claims
        self.start(answer(ties_to_total=False, difference_pence=99999))
        page = self.client.get(self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk))
        self.assertContains(page, 'Ties exactly')
        with org_context(self.org_a):
            Package.objects.filter(pk=self.pkg.pk).update(quote_value=None)  # nothing typed: tie to the total read
        self.start(answer(quote_total_pence=1_215_000))
        page = self.client.get(self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk))
        self.assertContains(page, 'Difference £150.00')
        # And no rival's price on the page
        self.assertNotContains(page, '47.77')
        self.assertNotContains(page, 'Rival')


class AccessTests(Setup):
    def urls(self):
        a = (self.bid.pk, self.trade.pk, self.pkg.pk)
        return self.url('ai_quote_map', *a), self.url('ai_quote_map_start', *a)

    def test_review_page_role_matrix(self):
        self.start(answer())
        review, _ = self.urls()
        expected = {self.alice: 200, self.adam: 200, self.eve: 200, self.pat: 404, self.stan: 404,
                    self.carla: 403, self.connie: 403, self.sid: 403, self.bea: 403, self.nora: 403, self.bob: 404}
        for user, code in expected.items():
            with self.subTest(user=user.email):
                self.assertEqual(self.status_for(user, review), code)

    def test_nobody_but_estimating_can_start_or_accept(self):
        self.start(answer())
        review, start = self.urls()
        d = self.draft()
        with org_context(self.org_a):
            m1 = d.lines.get(line=self.l1)
        with fake_ai(answer()) as client:
            for user in (self.pat, self.stan, self.carla, self.connie, self.sid, self.bea, self.nora, self.bob):
                for url, data in ((start, {}), (review, {'action': 'accept_all'}),
                                  (review, {'action': 'accept_line', 'item': m1.pk})):
                    with self.subTest(user=user.email, url=url):
                        self.assertIn(self.status_for(user, url, 'post', data), (403, 404))
        self.assertEqual(client.requests, [])
        with org_context(self.org_a):
            self.assertFalse(QuoteCell.objects.filter(package=self.pkg).exists())
            self.assertEqual(AIRun.objects.count(), 1)

    def test_bidder_never_sees_the_levelling_or_the_ai_button(self):
        self.start(answer())
        self.login(self.bea)
        for url in (self.url('tender', self.bid.pk), self.url('needs_me'), self.url('dashboard')):
            r = self.client.get(url)
            with self.subTest(url=url):
                self.assertNotContains(r, 'ai-map', status_code=r.status_code)
                self.assertNotContains(r, 'Map with AI', status_code=r.status_code)
                self.assertNotContains(r, '47.77', status_code=r.status_code)
        self.assertEqual(self.status_for(self.bea, self.url('levelling_sheet', self.bid.pk, self.trade.pk)), 403)

    def test_sheet_shows_map_with_ai_to_estimators(self):
        self.login(self.eve)
        with fake_ai():
            page = self.client.get(self.url('levelling_sheet', self.bid.pk, self.trade.pk))
        self.assertContains(page, 'Map with AI')
        self.assertContains(page, self.urls()[1])

    def test_closed_bid_is_read_only_and_worker_refuses(self):
        self.login(self.eve)
        with fake_ai(answer()) as client:
            self.client.post(self.urls()[1])  # queued, not yet run
            with org_context(self.org_a):
                Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_LOST)
            run_worker()
        self.assertEqual(client.requests, [])
        with org_context(self.org_a):
            run = AIRun.objects.get()
            self.assertFalse(AIUsage.objects.exists())
        self.assertEqual(run.status, AIRun.FAILED)
        self.assertIn('no longer an open bid', run.message)

    def test_over_budget_sends_nothing(self):
        self.switch_on(monthly_cap=D('0.01'))
        r, client = self.start(answer(), counts=[3000])
        self.assertEqual(client.requests, [])


class DemoTests(Base):
    def test_tie_arithmetic(self):
        from types import SimpleNamespace
        line = SimpleNamespace(qty=D('8'))
        lines = [SimpleNamespace(line=line, rate=D('310'), amount=None, included=False)]
        draft = SimpleNamespace(unmatched=[{'amount': '320.00'}], quote_total=D('3000.00'),
                                package=SimpleNamespace(quote_value=None))
        t = quotemap.tie(draft, lines)
        self.assertEqual((t['mapped'], t['extra'], t['difference'], t['total'], t['ties']),
                         (D('2480.00'), D('320.00'), D('200.00'), D('3000.00'), False))
        self.assertEqual(json.loads(json.dumps(draft.unmatched))[0]['amount'], '320.00')
