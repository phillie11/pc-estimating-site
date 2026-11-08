"""
Regression tests for the independent review of the AI drafting branch (F1 to F8, and the worker's re-check).
No network: the client is ai.tests.base.FakeClient.
"""
import importlib
import logging
import os
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2
from django.test import override_settings

from ai import rates, registry, service
from ai.models import AIRun, AIUsage, CLARIFICATIONS, TENDER_BRIEF
from handoverpack import clarifications
from handoverpack.models import PackClarification
from projects.models import Project
from tenancy.context import org_context
from tenancy.models import Membership

from .base import Base, fake_ai, message, run_worker
from .test_brief import Setup as BriefSetup, brief_reply, pdf
from .test_clarifications import Setup as ClarSetup, reply as clar_reply

OK = {'clarifications': []}


class Calls(Base):
    def setUp(self):
        super().setUp()
        self.switch_on()

    def call(self, **kw):
        with org_context(self.org_a):
            return service.call(org=self.org_a, user=self.eve, task=CLARIFICATIONS, project=self.bid, run=None,
                                content='x' * 50, chars=50, **kw)

    def usage(self):
        with org_context(self.org_a):
            return AIUsage.objects.get()


class F1FallbackCosting(Calls):
    def test_every_attempt_is_costed_at_its_own_models_rate(self):
        msg = message(OK, model='claude-opus-5', tokens=(20000, 16000, 0, 0))
        msg.usage.iterations = [
            SimpleNamespace(type='message', model='claude-opus-5-5', input_tokens=20000, output_tokens=15000),
            SimpleNamespace(type='fallback_message', model='claude-opus-5', input_tokens=20000, output_tokens=16000)]
        with fake_ai(msg):
            self.call()
        u = self.usage()
        billed = (rates.cost_gbp('claude-opus-5-5', input_tokens=20000, output_tokens=15000)
                  + rates.cost_gbp('claude-opus-5', input_tokens=20000, output_tokens=16000))
        self.assertEqual(u.cost_gbp, billed)
        self.assertEqual((u.input_tokens, u.output_tokens), (40000, 31000))

    def test_fallback_is_off_by_default_and_holds_for_two_attempts_when_on(self):
        self.assertFalse(service.settings.AI_REFUSAL_FALLBACK)
        one = service.estimate('claude-opus-5-5', 16000, chars=100)
        with override_settings(AI_REFUSAL_FALLBACK=True):
            two = service.estimate('claude-opus-5-5', 16000, chars=100)
            with fake_ai(message(OK)) as client:
                self.call()
        self.assertEqual(client.requests[0]['fallbacks'], 'default')
        self.assertEqual(two, one + rates.dearest_cost_gbp(input_tokens=service.input_estimate(100),
                                                           output_tokens=16000))
        self.assertGreater(two, one * 2)  # the second attempt is held at the dearest rates


class F2PageCounts(BriefSetup):
    def big(self):
        # One page object a parser can see; a real file could keep hundreds more in compressed object streams
        with org_context(self.org_a):
            return self.doc(self.bid, 'KW-BIG', 'Big prelims', pdf(1) + b'%' + b'A' * (3 * 1024 * 1024))

    def test_without_a_token_count_size_sets_the_floor_and_the_limit_holds(self):
        big = self.big()
        r, client = self.read([big.pk], brief_reply())
        self.assertEqual(client.requests, [])  # 3 MB is over 100 pages at one per 30 KB: left out, nothing sent
        with org_context(self.org_a):
            self.assertFalse(AIUsage.objects.exists())
            self.assertIn('limited to about 100 pages', AIRun.objects.get().message)

    def test_the_hold_is_the_token_count_when_there_is_one(self):
        self.login(self.eve)
        with fake_ai(brief_reply(), counts=[40000, 20000]) as client:
            self.client.post(self.url('ai_brief_read', self.bid.pk), {'revisions': [self.spec.pk, self.prelims.pk]})
            run_worker()
        self.assertEqual(len(client.count_requests), 2)
        self.assertEqual(client.count_requests[0]['messages'][0]['content'][0]['type'], 'document')
        with org_context(self.org_a):
            u = AIUsage.objects.get()
        self.assertIn(f'{int(60000 * service.COUNT_MARGIN):,} tokens held', u.note)

    @override_settings(AI_TENDER_MAX_PAGES=10)
    def test_the_limit_uses_the_token_count_not_the_files_page_count(self):
        # Each file says 2 or 3 pages, but Anthropic counts 40,000 tokens (8 pages' worth) each: only one fits in 10
        self.login(self.eve)
        with fake_ai(brief_reply(), counts=[40000, 40000]) as client:
            self.client.post(self.url('ai_brief_read', self.bid.pk), {'revisions': [self.spec.pk, self.prelims.pk]})
            run_worker()
        docs = [b for b in client.requests[0]['messages'][0]['content'] if b['type'] == 'document']
        self.assertEqual(len(docs), 1)


class F3WhatClarificationsSend(ClarSetup):
    def test_supplier_names_money_and_phone_layouts_are_masked(self):
        with org_context(self.org_a):
            r = self.pack.workbook.result
            tr = next(t for t in r['trades'] if t['include'] and t['lines'] and t['suppliers'])
            supplier = next(s for s in tr['suppliers'] if s)
            ln = next(x for x in tr['lines'] if x['description'])
            ln['comment'] = f'Per {supplier} quote, net £4,200 after 2.5% discount, 1,350.00'
            r['joinery']['lines'][0]['notes'] = 'Workshop quote 3,450.00'
            r['prelims']['lines'][0]['basis'] = 'Company rate £1,650 a week'
            self.pack.site_rules = ('Contact +44 (0)20 7946 0000, landlord (020) 7946 0001, '
                                    'security 020-7946-0002, mobile +44 7700 900 123.')
            text, _ = clarifications.build_input(self.pack)
        self.assertNotIn(supplier, text)
        self.assertIn('[supplier]', text)
        for leak in ('4,200', '2.5%', '1,350.00', '3,450.00', '1,650', '7946', '900 123'):
            self.assertNotIn(leak, text)
        self.assertIn('186', text)  # quantities in the line itself still go
        self.assertIn('£5,000', text)  # and the provisional sum's own amount

    def test_profile_and_overheads_go_only_with_the_tender_reader(self):
        self.switch_on(overheads_profit=D('12.50'), profile='Offices and retail, £400k to £2.5m')
        r, client = self.draft(None, clar_reply(('Doors', 'Allowed 14 doorsets.', [])))
        system = client.requests[0]['system'][0]['text']
        self.assertNotIn('Overheads', system)
        self.assertNotIn('Offices and retail', system)
        with org_context(self.org_a):
            tender = registry.system_prompt(self.org_a, TENDER_BRIEF)
        self.assertIn('Overheads and profit: 12.50%', tender)
        self.assertIn('Offices and retail', tender)


class F4TieredRates(Base):
    def test_haiku_long_context_tier(self):
        with override_settings(AI_GBP_PER_USD='1'):
            short = rates.cost_gbp('claude-haiku-5-5', input_tokens=100_000, output_tokens=10_000)
            long = rates.cost_gbp('claude-haiku-5-5', input_tokens=500_000, output_tokens=32_000)
            cached = rates.cost_gbp('claude-haiku-5-5', input_tokens=10_000, cache_read_tokens=200_000)
        self.assertEqual(short, D('0.0150'))                       # $0.10 / $0.50 up to 100K
        self.assertEqual(long, D('0.3300'))                        # $0.50 / $2.50 beyond
        self.assertEqual(cached, D('0.0150'))                      # cached tokens count towards the 100K
        self.assertEqual(rates.dearest()['output'], D('25.00'))


class F5UnknownCost(Calls):
    def test_no_answer_after_sending_keeps_the_hold(self):
        req = httpx2.Request('POST', 'https://api.anthropic.com/v1/messages')
        with org_context(self.org_a):
            system = registry.system_prompt(self.org_a, CLARIFICATIONS)
        held = service.estimate('claude-opus-5-5', 16000, chars=50 + len(system))
        errors = [anthropic.APITimeoutError(request=req), anthropic.APIConnectionError(request=req),
                  httpx2.ReadTimeout('read', request=req), httpx2.RemoteProtocolError('dropped', request=req),
                  anthropic.InternalServerError('down', response=httpx2.Response(500, request=req), body=None)]
        with fake_ai(*errors):
            for _ in errors:
                with self.assertRaises(service.CallFailed):
                    self.call()
        with org_context(self.org_a):
            rows = list(AIUsage.objects.all())
        self.assertEqual(len(rows), 5)
        for u in rows:
            self.assertEqual((u.status, u.cost_gbp), (AIUsage.FAILED, held))
            self.assertIn(service.UNKNOWN_COST_NOTE, u.note)

    def test_a_read_timeout_while_streaming_is_caught(self):
        req = httpx2.Request('POST', 'https://api.anthropic.com/v1/messages')

        class Events:
            def __iter__(self):
                yield 'message_start'
                raise httpx2.ReadTimeout('stalled', request=req)

        msg = message(OK)
        msg.events = Events()
        with fake_ai(msg), self.assertRaises(service.CallFailed):
            self.call()
        self.assertIn(service.UNKNOWN_COST_NOTE, self.usage().note)

    @override_settings(AI_DEADLINE_SECONDS=0)
    def test_the_whole_answer_has_a_deadline(self):
        msg = message(OK)
        msg.events = ['message_start', 'content_block_delta']
        with fake_ai(msg), self.assertRaises(service.CallFailed):
            self.call()
        self.assertEqual(self.usage().status, AIUsage.FAILED)


class F6Logging(Base):
    def test_sdk_loggers_are_quieted_whenever_a_client_is_made(self):
        logging.getLogger('anthropic').setLevel(logging.DEBUG)
        logging.getLogger('httpx2').setLevel(logging.DEBUG)
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-test-dummy'}):
            service.get_client()
        self.assertEqual(logging.getLogger('anthropic').level, logging.WARNING)
        self.assertEqual(logging.getLogger('httpx2').level, logging.WARNING)

    def test_check_warns_when_anthropic_log_is_set(self):
        from ai.checks import sdk_logging
        with mock.patch.dict(os.environ, {'ANTHROPIC_LOG': 'debug'}):
            self.assertEqual([w.id for w in sdk_logging(None)], ['ai.W001'])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ANTHROPIC_LOG', None)
            self.assertEqual(sdk_logging(None), [])


class F7MigrationRollback(ClarSetup):
    def test_going_back_hides_unapproved_drafts(self):
        from django.apps import apps
        with org_context(self.org_a):
            draft = PackClarification.objects.create(pack=self.pack, source='DRAFT', approved=False, text='Draft')
            approved = PackClarification.objects.create(pack=self.pack, source='DRAFT', approved=True, text='OK')
        mig = importlib.import_module('handoverpack.migrations.0003_ai_draft_clarifications')
        mig.hide_unapproved_drafts(apps, None)
        with org_context(self.org_a):
            draft.refresh_from_db()
            approved.refresh_from_db()
        self.assertTrue(draft.hidden)
        self.assertFalse(approved.hidden)
        last = mig.Migration.operations[-1]
        self.assertEqual(last.reverse_code, mig.hide_unapproved_drafts)  # runs first on the way back


class F8UsageMonth(Base):
    def test_odd_months_show_this_month(self):
        self.login(self.alice)
        for bad in ('0000-01', '9999-12', '2026-13', '２０２６-01', 'x'):
            with self.subTest(bad):
                self.assertEqual(self.client.get(self.url('ai_usage') + f'?month={bad}').status_code, 200)


class WorkerRecheck(ClarSetup):
    def test_a_bid_no_longer_open_is_not_read(self):
        with org_context(self.org_a):
            run = AIRun.objects.create(project=self.bid, feature=TENDER_BRIEF, requested_by=self.eve,
                                       params={'revisions': []})
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_LOST)
        from ai.tasks import run_job
        with fake_ai(brief_reply(), counts=[1000]) as client, org_context(self.org_a):
            run_job({'run_id': run.pk})
            run.refresh_from_db()
            self.assertFalse(AIUsage.objects.exists())
        self.assertEqual((client.requests, client.count_requests), ([], []))
        self.assertEqual(run.status, AIRun.FAILED)
        self.assertIn('no longer an open bid', run.message)

    def test_a_requester_who_lost_access_is_not_charged(self):
        with org_context(self.org_a):
            brief_run = AIRun.objects.create(project=self.bid, feature=TENDER_BRIEF, requested_by=self.eve,
                                             params={'revisions': []})
            pack_run = AIRun.objects.create(project=self.bid, feature=CLARIFICATIONS, requested_by=self.eve)
        Membership.objects.filter(organization=self.org_a, user=self.eve).update(area=Membership.AREA_DELIVERY)
        from ai.tasks import run_job
        with fake_ai(brief_reply(), clar_reply(('Doors', 'x', []))) as client, org_context(self.org_a):
            for run in (brief_run, pack_run):
                run_job({'run_id': run.pk})
                run.refresh_from_db()
                self.assertEqual(run.status, AIRun.FAILED)
                self.assertIn('no longer', run.message)
            self.assertFalse(AIUsage.objects.exists())
        self.assertEqual(client.requests, [])
