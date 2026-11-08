"""
Who reaches the AI pages: owners and admins for settings, playbooks and usage; estimators for the bid brief; the
pack's own rule for drafting clarifications. Clients, subcontractors, bidders and other companies never see any
of it: not the pages, not the portal, not the API, not the activity feed.
"""
from decimal import Decimal as D

from ai.models import AIRun, AISettings, AIUsage, BidBrief, BriefItem, CLARIFICATIONS, Playbook
from ai import registry
from handoverpack import services
from handoverpack.tests.test_views import upload_file
from tenancy.context import org_context
from tenancy.models import ActivityEvent

from .base import Base, fake_ai


class Setup(Base):
    def setUp(self):
        super().setUp()
        self.switch_on()
        with org_context(self.org_a):
            services.upload(self.bid, self.eve, upload_file())
            self.brief = BidBrief.objects.create(project=self.bid, summary='Brief summary', ai_drafted=True)
            self.item = BriefItem.objects.create(brief=self.brief, section='risk', text='A risk', ai_drafted=True)

    def everyone(self, base):
        out = {self.carla: 403, self.connie: 403, self.sid: 403, self.bea: 403, self.nora: 403}
        out.update(base)
        return out


class SettingsAccessTests(Setup):
    def test_settings_playbooks_and_usage_are_for_owners_and_admins(self):
        urls = [self.url('ai_settings'), self.url('ai_usage'), self.url('ai_usage') + '?export=csv',
                self.url('ai_playbook', CLARIFICATIONS), self.url('ai_playbook', 'tender_brief')]
        expected = self.everyone({self.alice: 200, self.adam: 200, self.eve: 403, self.pat: 403, self.stan: 403})
        for url in urls:
            for user, code in expected.items():
                with self.subTest(url=url, user=user.email):
                    self.assertEqual(self.status_for(user, url), code)
            with self.subTest(url=url, user='bob'):
                self.assertIn(self.status_for(self.bob, url), (403, 404))
        self.assertEqual(self.status_for(self.alice, self.url('ai_playbook', 'no_such_task')), 404)

    def test_only_owners_and_admins_change_settings(self):
        for user in (self.eve, self.pat, self.carla):
            self.status_for(user, self.url('ai_settings'), 'post', {'enabled': '', 'monthly_cap': '999', 'who': 'STAFF'})
        with org_context(self.org_a):
            s = AISettings.for_org(self.org_a)
        self.assertTrue(s.enabled)
        self.assertEqual(s.monthly_cap, D('20.00'))
        self.assertEqual(self.status_for(self.adam, self.url('ai_settings'), 'post',
                                         {'monthly_cap': '35', 'who': 'ADMINS'}), 302)
        with org_context(self.org_a):
            s = AISettings.for_org(self.org_a)
        self.assertEqual((s.enabled, s.monthly_cap, s.who, s.updated_by), (False, D('35'), 'ADMINS', self.adam))
        self.assertEqual(self.status_for(self.adam, self.url('ai_settings'), 'post',
                                         {'monthly_cap': '5000', 'who': 'ADMINS'}), 200)  # over the ceiling

    def test_playbook_versions_reset_and_cap(self):
        url = self.url('ai_playbook', CLARIFICATIONS)
        self.login(self.alice)
        self.client.post(url, {'text': 'Our way: name the floor.'})
        self.client.post(url, {'text': 'Our way, version two.'})
        self.client.post(url, {'reset': '1'})
        self.client.post(url, {'text': 'x' * (registry.PLAYBOOK_MAX_CHARS + 1)})  # refused
        with org_context(self.org_a):
            versions = list(Playbook.objects.filter(task=CLARIFICATIONS).order_by('version').values_list(
                'version', 'text', 'created_by'))
            text, own = registry.playbook_for(self.org_a, CLARIFICATIONS)
        self.assertEqual(versions, [(1, 'Our way: name the floor.', self.alice.pk),
                                    (2, 'Our way, version two.', self.alice.pk), (3, '', self.alice.pk)])
        self.assertFalse(own)
        self.assertEqual(text, registry.get(CLARIFICATIONS).default_playbook)
        page = self.client.get(url)
        self.assertContains(page, 'Changes')
        with org_context(self.org_b):
            self.assertFalse(Playbook.objects.exists())

    def test_usage_page_and_csv_are_this_companys_and_safe(self):
        with org_context(self.org_a):
            self.bid.name = '=HYPERLINK("http://evil.example")'
            self.bid.save()
            AIUsage.objects.create(user=self.eve, feature=CLARIFICATIONS, project=self.bid, model='claude-opus-5-5',
                                   status=AIUsage.OK, input_tokens=100, output_tokens=50, cost_gbp=D('1.2345'))
        with org_context(self.org_b):
            AIUsage.objects.create(user=self.bob, feature=CLARIFICATIONS, model='claude-opus-5-5', status=AIUsage.OK,
                                   cost_gbp=D('9.99'), note='BRAVO USAGE')
        self.login(self.alice)
        page = self.client.get(self.url('ai_usage'))
        self.assertContains(page, 'Eve Estimator')
        self.assertContains(page, '1.23')
        self.assertNotContains(page, 'BRAVO')
        csv = self.client.get(self.url('ai_usage') + '?export=csv').content.decode()
        self.assertIn("'=HYPERLINK", csv)
        self.assertNotIn('BRAVO', csv)


class BriefAccessTests(Setup):
    def pages(self):
        pk = self.bid.pk
        return [self.url('ai_brief', pk), self.url('ai_brief', pk) + '?export=csv',
                self.url('ai_brief_item', pk, self.item.pk)]

    def test_bid_brief_is_for_estimating_staff(self):
        expected = self.everyone({self.alice: 200, self.adam: 200, self.eve: 200, self.pat: 404, self.stan: 404,
                                  self.bob: 404})
        for url in self.pages():
            for user, code in expected.items():
                with self.subTest(url=url, user=user.email):
                    self.assertEqual(self.status_for(user, url), code)

    def test_nobody_outside_can_write(self):
        pk = self.bid.pk
        posts = [self.url('ai_brief_read', pk), self.url('ai_brief_item', pk, self.item.pk),
                 self.url('ai_brief_accept', pk, self.item.pk), self.url('ai_brief', pk)]
        with fake_ai() as client:
            for url in posts:
                for user in (self.carla, self.bea, self.sid, self.nora, self.pat, self.bob):
                    with self.subTest(url=url, user=user.email):
                        self.assertIn(self.status_for(user, url, 'post', {'delete': '1', 'text': 'x'}), (403, 404))
        self.assertEqual(client.requests, [])
        with org_context(self.org_a):
            self.assertTrue(BriefItem.objects.filter(pk=self.item.pk).exists())
            self.assertFalse(AIRun.objects.exists())

    def test_brief_on_a_live_job_is_not_found(self):
        self.assertEqual(self.status_for(self.alice, self.url('ai_brief', self.pa.pk)), 404)

    def test_tab_shows_for_estimators_only(self):
        self.login(self.eve)
        self.assertContains(self.client.get(self.url('project_detail', self.bid.pk)), self.url('ai_brief', self.bid.pk))


class DraftClarificationsAccessTests(Setup):
    def test_only_pack_staff_who_may_use_ai_can_start_a_draft(self):
        url = self.url('ai_draft_clarifications', self.bid.pk)
        with fake_ai() as client:
            for user, code in self.everyone({self.pat: 404, self.stan: 404, self.bob: 404}).items():
                with self.subTest(user=user.email):
                    self.assertEqual(self.status_for(user, url, 'post'), code)
            self.assertEqual(self.status_for(self.eve, url, 'get'), 405)
            self.assertEqual(self.status_for(self.eve, url, 'post'), 302)
        self.assertEqual(client.requests, [])  # the page only queues; the worker calls
        with org_context(self.org_a):
            self.assertEqual(AIRun.objects.count(), 1)

    def test_invisible_to_externals_in_portals_api_and_feed(self):
        with fake_ai():
            self.login(self.eve)
            self.client.post(self.url('ai_draft_clarifications', self.bid.pk))
        from django.test import Client
        self.client = Client()  # Eve's flash message stays in her own browser
        for user in (self.carla, self.sid):
            self.login(user)
            for url in (self.url('project_detail', self.bid.pk), self.url('needs_me'), self.url('activity'),
                        self.url('dashboard')):
                r = self.client.get(url)
                with self.subTest(user=user.email, url=url):
                    self.assertNotContains(r, '/bid-brief/', status_code=r.status_code)
                    self.assertNotContains(r, '/clarifications/draft/', status_code=r.status_code)
                    self.assertNotContains(r, 'AI draft', status_code=r.status_code)
        with org_context(self.org_a):
            self.assertFalse(ActivityEvent.objects.filter(verb__contains='AI', internal_only=False).exists())
        from api.resources import resources
        models = {getattr(r, 'model', None) for r in resources()}
        for m in (AISettings, AIUsage, AIRun, BidBrief, BriefItem, Playbook):
            self.assertNotIn(m, models)
