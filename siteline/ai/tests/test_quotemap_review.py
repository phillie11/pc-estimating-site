"""Regression tests for the quote-mapping review (Q1 to Q7). No network: FakeClient only."""
from decimal import Decimal as D
from unittest import mock

from django.core.files.base import ContentFile

from ai import service
from ai.models import AIRun, AIUsage, QuoteMapDraft, QuoteMapLine
from directory.models import Package
from levelling import services as levelling
from levelling.models import QuoteCell
from tenancy.context import org_context

from .base import fake_ai, run_worker
from .test_brief import Setup as BriefSetup, brief_reply, pdf
from .test_quotemap import Setup, answer, xlsx_bytes


class Q1NothingLeavesBeforeTheChecks(Setup):
    def test_switched_off_after_queueing_makes_no_client_call_of_any_kind(self):
        self.login(self.eve)
        with fake_ai(answer(), counts=[1234]) as client, \
                mock.patch('ai.service.get_client', wraps=lambda: client) as made:
            self.client.post(self.url('ai_quote_map_start', self.bid.pk, self.trade.pk, self.pkg.pk))
            self.switch_on(enabled=False)
            run_worker()
        self.assertEqual((client.requests, client.count_requests, made.call_count), ([], [], 0))
        with org_context(self.org_a):
            run = AIRun.objects.get()
            self.assertFalse(AIUsage.objects.exists())
        self.assertEqual(run.status, AIRun.FAILED)
        self.assertIn('switched off', run.message)

    def test_a_requester_no_longer_allowed_makes_no_call(self):
        self.login(self.eve)
        with fake_ai(answer(), counts=[1234]) as client:
            self.client.post(self.url('ai_quote_map_start', self.bid.pk, self.trade.pk, self.pkg.pk))
            self.switch_on(who='ADMINS')
            run_worker()
        self.assertEqual((client.requests, client.count_requests), ([], []))

    def test_budget_used_up_makes_no_call(self):
        self.login(self.eve)
        with fake_ai(answer(), counts=[1234]) as client:
            self.client.post(self.url('ai_quote_map_start', self.bid.pk, self.trade.pk, self.pkg.pk))
            with org_context(self.org_a):
                AIUsage.objects.create(feature='quote_mapping', model='m', status=AIUsage.OK, cost_gbp=D('20'))
            run_worker()
        self.assertEqual((client.requests, client.count_requests), ([], []))


class Q1TenderReaderToo(BriefSetup):
    def test_switched_off_tender_reading_makes_no_call(self):
        self.login(self.eve)
        with fake_ai(brief_reply(), counts=[1000]) as client:
            self.client.post(self.url('ai_brief_read', self.bid.pk), {'revisions': [self.spec.pk]})
            self.switch_on(enabled=False)
            run_worker()
        self.assertEqual((client.requests, client.count_requests), ([], []))

    def test_count_tokens_refuses_without_the_checks_passing(self):
        self.switch_on(enabled=False)
        with fake_ai(counts=[1]) as client, org_context(self.org_a):
            with self.assertRaises(service.Unavailable):
                service.count_tokens([{'type': 'text', 'text': 'x'}], org=self.org_a, user=self.eve)
        self.assertEqual(client.count_requests, [])


class Q2StaleDraft(Setup):
    def replace_quote(self, data=None, name='quote-a-rev2.pdf'):
        with org_context(self.org_a):
            self.pkg.quote_file.save(name, ContentFile(data or pdf(2, b'REVISED')), save=True)

    def test_identity_is_stored(self):
        self.start(answer(), counts=[1000])
        d = self.draft()
        self.assertEqual(d.quote_name, self.pkg.quote_file.name)
        self.assertEqual(len(d.quote_sha256), 64)
        self.assertGreater(d.quote_size, 0)

    def test_a_replaced_quote_makes_the_draft_stale_and_refuses_accepts(self):
        self.start(answer(), counts=[1000])
        self.replace_quote()
        url = self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk)
        page = self.client.get(url)
        self.assertContains(page, 'Out of date')
        self.assertContains(page, 'Map the new quote')
        self.assertNotContains(page, 'Accept everything still to check')
        d = self.draft()
        with org_context(self.org_a):
            m = d.lines.first()
        for data in ({'action': 'accept_all'}, {'action': 'accept_line', 'item': m.pk}):
            self.client.post(url, data)
        with org_context(self.org_a):
            self.assertFalse(QuoteCell.objects.filter(package=self.pkg).exists())

    def test_same_name_same_size_different_bytes_is_caught_on_accept(self):
        self.start(answer(), counts=[1000])
        d = self.draft()
        with org_context(self.org_a):
            with self.pkg.quote_file.open('rb') as fh:
                old = fh.read()
            storage, name = self.pkg.quote_file.storage, self.pkg.quote_file.name
            storage.delete(name)
            storage.save(name, ContentFile(old.replace(b'OUR-BIDDER-QUOTE', b'OUR-BIDDER-QUOTX')))
        self.client.post(self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk), {'action': 'accept_all'})
        with org_context(self.org_a):
            self.assertFalse(QuoteCell.objects.filter(package=self.pkg).exists())
            d.refresh_from_db()
        self.assertEqual(d.status, QuoteMapDraft.OPEN)


class Q3TieToTheQuoteValue(Setup):
    def test_tied_to_the_package_value_and_flags_a_different_read_total(self):
        lines = [{'scope_key': 'S1', 'rate_pence': 3500, 'amount_pence': -1, 'included_elsewhere': False,
                  'source': 'p1', 'note': ''},
                 {'scope_key': 'S2', 'rate_pence': -1, 'amount_pence': 200000, 'included_elsewhere': False,
                  'source': 'p1', 'note': ''}]
        self.start(answer(lines=lines, quote_total_pence=900_000), counts=[1000])
        page = self.client.get(self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk))
        self.assertNotContains(page, 'Ties exactly')
        self.assertContains(page, 'Difference £3,000.00')  # 12,000 quote value less 9,000 mapped
        self.assertContains(page, 'differ')

    def test_lines_tie_to_the_value_but_the_read_total_differs(self):
        self.start(answer(quote_total_pence=1_100_000), counts=[1000])  # lines 12,000 = value; read 11,000
        page = self.client.get(self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk))
        self.assertNotContains(page, 'Ties exactly')
        self.assertContains(page, 'and the total read from the quote')


class Q4WorkbookTokens(Setup):
    def test_workbook_text_is_counted_and_held(self):
        with org_context(self.org_a):
            self.pkg.quote_file.save('quote.xlsx', ContentFile(xlsx_bytes([['1', 'Stud', 52.5]])), save=True)
        r, client = self.start(answer(), counts=[777])
        self.assertEqual(len(client.count_requests), 1)
        self.assertEqual(client.count_requests[0]['messages'][0]['content'][0]['type'], 'text')
        with org_context(self.org_a):
            self.assertIn(f'{int(777 * service.COUNT_MARGIN):,} tokens held', AIUsage.objects.get().note)

    def test_without_a_count_a_token_a_character(self):
        with org_context(self.org_a):
            self.pkg.quote_file.save('quote.csv', ContentFile(b'Item,Rate\n1,52.5\n'), save=True)
        r, client = self.start(answer())  # no counts: counting fails
        with org_context(self.org_a):
            note = AIUsage.objects.get().note
        sent = client.requests[0]['messages'][0]['content'][0]['text']
        self.assertIn(f'{len(sent):,} tokens held', note)


class Q5CsvErrors(Setup):
    def test_a_long_csv_field_is_a_plain_message(self):
        with org_context(self.org_a):
            self.pkg.quote_file.save('quote.csv', ContentFile(b'Item,"' + b'x' * 200_000 + b'"\n'), save=True)
        r, client = self.start(answer())
        with org_context(self.org_a):
            run = AIRun.objects.get()
        self.assertEqual(client.requests, [])
        self.assertIn('CSV couldn\'t be read', run.message)
        self.assertNotIn('Siteline\'s side', run.message)


class Q6Locking(Setup):
    def test_decisions_lock_the_draft_and_only_take_pending_rows(self):
        self.start(answer(), counts=[1000])
        d = self.draft()
        url = self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk)
        with org_context(self.org_a):
            m = d.lines.first()
        from django.db.models.query import QuerySet
        locked = []
        real = QuerySet.select_for_update

        def spy(qs, *a, **kw):
            locked.append(qs.model.__name__)
            return real(qs, *a, **kw)
        with mock.patch.object(QuerySet, 'select_for_update', spy):
            self.client.post(url, {'action': 'accept_line', 'item': m.pk})
        self.assertIn('QuoteMapDraft', locked)
        self.client.post(url, {'action': 'accept_line', 'item': m.pk})  # already accepted: a 404, not a second write
        with org_context(self.org_a):
            self.assertEqual(QuoteCell.objects.filter(package=self.pkg).count(), 1)


class Q7ExistingPrices(Setup):
    def test_accept_all_keeps_a_different_existing_price_and_lists_it(self):
        self.start(answer(), counts=[1000])
        with org_context(self.org_a):
            levelling.set_cell(self.l1, self.pkg, rate=D('61.00'))
        url = self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk)
        page = self.client.get(url)
        self.assertContains(page, '£12,200.00')  # what the levelling holds now for S1 (61 x 200)
        self.assertContains(page, 'Replace')
        r = self.client.post(url, {'action': 'accept_all'}, follow=True)
        self.assertContains(r, 'which was kept')
        with org_context(self.org_a):
            self.assertEqual(QuoteCell.objects.get(package=self.pkg, line=self.l1).rate, D('61.00'))
            self.assertEqual(QuoteCell.objects.get(package=self.pkg, line=self.l2).amount, D('2000.00'))
            self.assertEqual(QuoteMapLine.objects.get(line=self.l1).status, QuoteMapLine.KEPT)

    def test_an_individual_accept_overwrites(self):
        self.start(answer(), counts=[1000])
        with org_context(self.org_a):
            levelling.set_cell(self.l1, self.pkg, rate=D('61.00'))
            m = self.draft().lines.get(line=self.l1)
        self.client.post(self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk),
                         {'action': 'accept_line', 'item': m.pk})
        with org_context(self.org_a):
            self.assertEqual(QuoteCell.objects.get(package=self.pkg, line=self.l1).rate, D('50.00'))

    def test_declined_package_accepts_nothing(self):
        self.start(answer(), counts=[1000])
        with org_context(self.org_a):
            Package.objects.filter(pk=self.pkg.pk).update(status=Package.DECLINED)
        url = self.url('ai_quote_map', self.bid.pk, self.trade.pk, self.pkg.pk)
        self.client.post(url, {'action': 'accept_all'})
        with org_context(self.org_a):
            self.assertFalse(QuoteCell.objects.filter(package=self.pkg).exists())
