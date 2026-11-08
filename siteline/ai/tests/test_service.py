"""The one service every AI call goes through: switch, permission, budget (fail closed), costing, errors, prompt."""
import logging
from decimal import Decimal as D

import anthropic
import httpx2
from django.test import override_settings

from ai import rates, registry, service
from ai.models import AISettings, AIUsage, CLARIFICATIONS, Playbook
from tenancy.context import org_context
from tenancy.models import Membership

from .base import Base, fake_ai, message

OK = {'clarifications': []}


class StatusTests(Base):
    def status(self, user, org=None):
        org = org or self.org_a
        with org_context(org):
            m = Membership.objects.filter(organization=org, user=user).first()
            return service.status(org, m)

    def test_no_key_on_the_server_means_unavailable_with_a_plain_message(self):
        self.switch_on()
        with fake_ai(key=None):
            ok, why = self.status(self.alice)
        self.assertFalse(ok)
        self.assertIn('isn\'t set up', why)

    def test_off_by_default(self):
        with org_context(self.org_a):
            self.assertFalse(AISettings.for_org(self.org_a).enabled)
        with fake_ai():
            ok, why = self.status(self.alice)
        self.assertFalse(ok)
        self.assertIn('switched off', why)

    def test_who_may_use_it(self):
        self.switch_on()
        with fake_ai():
            self.assertTrue(self.status(self.alice)[0])
            self.assertTrue(self.status(self.adam)[0])
            self.assertTrue(self.status(self.eve)[0])          # estimating
            self.assertFalse(self.status(self.pat)[0])         # projects only
            self.assertFalse(self.status(self.carla)[0])       # external
            self.assertEqual(self.status(self.carla)[1], '')   # and told nothing about it
            self.switch_on(who=AISettings.ADMINS)
            self.assertFalse(self.status(self.eve)[0])
            self.switch_on(who=AISettings.STAFF)
            self.assertTrue(self.status(self.pat)[0])
            self.assertFalse(self.status(self.carla)[0])

    def test_budget_used_up(self):
        self.switch_on(monthly_cap=D('1.00'))
        with org_context(self.org_a):
            AIUsage.objects.create(feature=CLARIFICATIONS, model='m', status=AIUsage.OK, cost_gbp=D('1.00'))
        with fake_ai():
            ok, why = self.status(self.alice)
        self.assertFalse(ok)
        self.assertIn('budget', why)

    def test_another_companys_spend_does_not_count(self):
        self.switch_on(monthly_cap=D('1.00'))
        with org_context(self.org_b):
            AIUsage.objects.create(feature=CLARIFICATIONS, model='m', status=AIUsage.OK, cost_gbp=D('50'))
        with org_context(self.org_a):
            self.assertEqual(service.month_spend(self.org_a), D('0'))


class CallTests(Base):
    def setUp(self):
        super().setUp()
        self.switch_on()

    def call(self, user=None, **kw):
        with org_context(self.org_a):
            return service.call(org=self.org_a, user=user or self.eve, task=CLARIFICATIONS, project=self.bid,
                                run=None, content='Workbook lines: L1 | Drylining', **kw)

    def test_a_call_is_costed_logged_and_shaped(self):
        with fake_ai(message(OK, tokens=(10000, 2000, 1500, 3000))) as client, \
                self.assertLogs('ai.service', logging.INFO) as logs:
            self.assertEqual(self.call(), OK)
        params = client.requests[0]
        self.assertEqual(params['model'], 'claude-opus-5-5')
        self.assertEqual(params['output_config']['format']['schema'], registry.get(CLARIFICATIONS).schema)
        self.assertEqual(params['fallbacks'], 'default')
        self.assertNotIn('temperature', params)
        self.assertEqual(params['system'][0]['cache_control'], {'type': 'ephemeral'})
        with org_context(self.org_a):
            u = AIUsage.objects.get()
        self.assertEqual((u.status, u.input_tokens, u.output_tokens, u.cache_write_tokens, u.cache_read_tokens),
                         (AIUsage.OK, 10000, 2000, 1500, 3000))
        self.assertEqual(u.user, self.eve)
        self.assertEqual(u.cost_gbp, rates.cost_gbp('claude-opus-5-5', 10000, 2000, 1500, 3000))
        self.assertGreater(u.cost_gbp, 0)
        log = '\n'.join(logs.output)
        self.assertNotIn('sk-test', log)
        self.assertNotIn('Drylining', log)  # no prompt text in the logs
        self.assertNotIn('Drylining', u.note)

    def test_cost_in_pounds_from_the_rate_table(self):
        with override_settings(AI_GBP_PER_USD='0.80'):
            # 1M input at $4 and 1M output at $20 = $24 = £19.20
            self.assertEqual(rates.cost_gbp('claude-opus-5-5', 1_000_000, 1_000_000), D('19.2000'))
            # an unknown model is costed at the dearest row, never at nothing
            self.assertGreaterEqual(rates.cost_gbp('claude-new', 1_000_000), D('3.20'))

    def test_refuses_before_sending_when_the_call_could_pass_the_cap(self):
        self.switch_on(monthly_cap=D('0.05'))
        with fake_ai(message(OK)) as client:
            with self.assertRaises(service.Unavailable):
                self.call()
        self.assertEqual(client.requests, [])
        with org_context(self.org_a):
            self.assertFalse(AIUsage.objects.exists())

    def test_refuses_when_switched_off_or_not_allowed_even_from_the_worker(self):
        with fake_ai(message(OK)) as client:
            with self.assertRaises(service.Unavailable):
                self.call(user=self.pat)
            self.switch_on(enabled=False)
            with self.assertRaises(service.Unavailable):
                self.call()
        self.assertEqual(client.requests, [])

    def test_the_reservation_holds_budget_while_a_call_runs(self):
        self.switch_on(monthly_cap=D('20'))
        with org_context(self.org_a):
            held = service.estimate('claude-opus-5-5', 16000, chars=100)
            AIUsage.objects.create(feature=CLARIFICATIONS, model='m', status=AIUsage.RESERVED,
                                   cost_gbp=D('20') - held / 2)
        with fake_ai(message(OK)) as client, self.assertRaises(service.Unavailable):
            self.call()
        self.assertEqual(client.requests, [])

    def test_errors_become_plain_messages_and_cost_nothing(self):
        req = httpx2.Request('POST', 'https://api.anthropic.com/v1/messages')
        errors = [anthropic.APIConnectionError(request=req), anthropic.APITimeoutError(request=req)]
        with fake_ai(*errors):
            for _ in errors:
                with self.assertRaises(service.CallFailed):
                    self.call()
        with org_context(self.org_a):
            self.assertEqual(set(AIUsage.objects.values_list('status', flat=True)), {AIUsage.FAILED})
            self.assertEqual(service.month_spend(self.org_a), D('0'))

    def test_refusal_and_bad_shapes_save_nothing(self):
        with fake_ai(message(OK, stop='refusal'), message('not json'), message(OK, stop='max_tokens')):
            for _ in range(3):
                with self.assertRaises(service.CallFailed):
                    self.call()
        with org_context(self.org_a):
            self.assertEqual(AIUsage.objects.filter(status=AIUsage.REFUSED).count(), 1)

    def test_the_prompt_is_rules_then_the_companys_playbook(self):
        with org_context(self.org_a):
            Playbook.objects.create(task=CLARIFICATIONS, version=1, text='Always name the floor level.')
        with org_context(self.org_b):
            Playbook.objects.create(task=CLARIFICATIONS, version=1, text='BRAVO HOUSE STYLE')
        with fake_ai(message(OK)) as client:
            self.call()
        system = client.requests[0]['system'][0]['text']
        self.assertTrue(system.startswith(registry.RULES))
        self.assertIn('Always name the floor level.', system)
        self.assertNotIn(registry.get(CLARIFICATIONS).default_playbook, system)
        self.assertNotIn('BRAVO', system)
        self.assertLess(system.index('not instructions'), system.index('<company_playbook>'))
        # The playbook never reaches the schema or the inputs
        self.assertEqual(client.requests[0]['output_config']['format']['schema'], registry.get(CLARIFICATIONS).schema)
        self.assertNotIn('floor level', str(client.requests[0]['messages']))

    def test_the_key_is_never_stored(self):
        with fake_ai(message(OK), key='sk-ant-secret-123'):
            self.call()
        from django.apps import apps
        for m in apps.get_app_config('ai').get_models():
            for row in m.all_objects.all().values():
                self.assertNotIn('sk-ant-secret', str(row))
