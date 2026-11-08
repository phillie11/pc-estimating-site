"""
AI RFI drafting: what is sent, how drafts are checked and saved, that a draft reaches nobody outside until staff
issue it (register, detail, project page, search, Needs me, notifications), who may draft, and the budget.
"""
import base64
from decimal import Decimal as D

from ai.models import AIRun, AIUsage, BidBrief, BriefItem
from projects.models import Project
from rfis.models import RFI
from tenancy.context import org_context
from tenancy.models import Notification

from .base import fake_ai, message, run_worker
from .test_brief import Setup as BriefSetup


def rfi(subject='Ceiling grid conflict', question='Drawing KW-A-300 shows MF plasterboard; the spec says 600 grid. '
        'We have priced MF. Which is right?', why='Changes the ceiling rate on 410 m2.', category='DRAWING',
        priority='HIGH', doc=1, page=2):
    return {'subject': subject, 'question': question, 'why': why, 'category': category, 'priority': priority,
            'doc': doc, 'page': page}


def reply(*items):
    return message({'rfis': list(items)})


class Setup(BriefSetup):
    def draft(self, *replies, user=None, use_brief=True, ids=None, counts=None):
        self.login(user or self.eve)
        data = {'revisions': ids or [self.spec.pk]}
        if use_brief:
            data['use_brief'] = '1'
        with fake_ai(*replies, counts=counts) as client:
            r = self.client.post(self.url('ai_rfi_draft', self.bid.pk), data)
            run_worker()
        return r, client

    def rfis(self):
        with org_context(self.org_a):
            return list(RFI.objects.filter(project=self.bid, ai_drafted=True).order_by('number'))


class DraftTests(Setup):
    def test_drafts_are_internal_ai_drafts_with_their_source(self):
        r, client = self.draft(reply(rfi()))
        self.assertRedirects(r, self.url('ai_brief', self.bid.pk) + '#rfis', fetch_redirect_response=False)
        [d] = self.rfis()
        self.assertEqual((d.status, d.visibility, d.ai_drafted), (RFI.STATUS_DRAFT, RFI.VIS_INTERNAL, True))
        self.assertEqual(d.document_reference, 'KW-SPEC Specification rev T1, page 2')
        self.assertEqual((d.category, d.priority, d.created_by), ('DRAWING', 'HIGH', self.eve))
        self.assertEqual(d.ai_basis, 'Changes the ceiling rate on 410 m2.')
        self.assertIsNone(d.ball_in_court_user)
        with org_context(self.org_a):
            self.assertEqual(AIUsage.objects.get().feature, 'rfi_drafting')
            self.assertFalse(Notification.objects.exists())

    def test_validation_and_caps(self):
        with org_context(self.org_a):
            RFI(project=self.bid, title='Already asked about doors', question='q').save(actor=self.eve)
        self.draft(reply(
            rfi(subject='Already asked about doors'),                      # repeat of an existing subject
            rfi(subject='', question='No subject'),                        # dropped
            rfi(subject='No question', question=''),                       # dropped
            rfi(subject='Odd lists', category='NONSENSE', priority='ASAP', doc=7, page=999),
            rfi(subject='S' * 500, question='Q' * 5000, why='W' * 5000),
            rfi(subject='Odd lists', question='duplicate subject in one answer'),
            *[rfi(subject=f'Bulk {i}') for i in range(60)]))
        drafts = self.rfis()
        self.assertEqual(len(drafts), 40)
        odd = next(d for d in drafts if d.title == 'Odd lists')
        self.assertEqual((odd.category, odd.priority, odd.document_reference), ('OTHER', 'NORMAL', ''))
        long = next(d for d in drafts if d.title.startswith('SSS'))
        self.assertEqual((len(long.title), len(long.question), len(long.ai_basis)), (120, 2000, 1000))
        self.assertNotIn('Already asked about doors', [d.title for d in drafts])

    def test_what_is_sent(self):
        with org_context(self.org_a):
            RFI(project=self.bid, title='Existing question on lighting', question='q').save(actor=self.eve)
            RFI(project=self.pa, title='OTHER PROJECT RFI', question='q').save(actor=self.pat)
            b = BidBrief.objects.create(project=self.bid)
            BriefItem.objects.create(brief=b, section='risk', label='Uncapped LADs', text='LADs uncapped.')
            BriefItem.objects.create(brief=b, section='pack_missing', text='Asbestos survey: not in pack.')
            BriefItem.objects.create(brief=b, section='scope', text='SCOPE LINE NOT SENT')
        r, client = self.draft(reply(rfi()), ids=[self.spec.pk])
        content = client.requests[0]['messages'][0]['content']
        docs = [base64.b64decode(b['source']['data']) for b in content if b['type'] == 'document']
        text = ' '.join(b['text'] for b in content if b['type'] == 'text')
        self.assertEqual(len(docs), 1)
        self.assertIn(b'SPEC-MARKER', docs[0])
        self.assertIn('Existing question on lighting', text)
        self.assertIn('LADs uncapped.', text)
        self.assertIn('Asbestos survey', text)
        for leak in ('OTHER PROJECT RFI', 'SCOPE LINE NOT SENT', 'Eve', 'PRELIMS-MARKER'):
            self.assertNotIn(leak, text)
        r, client = self.draft(reply(rfi(subject='Another')), use_brief=False)
        text = ' '.join(b['text'] for b in client.requests[0]['messages'][0]['content'] if b['type'] == 'text')
        self.assertNotIn('LADs uncapped.', text)

    def test_over_budget_sends_nothing(self):
        self.switch_on(monthly_cap=D('0.01'))
        r, client = self.draft(reply(rfi()), counts=[3000])
        self.assertEqual(client.requests, [])
        self.assertEqual(self.rfis(), [])

    def test_switched_off_after_queueing_makes_no_call_of_any_kind(self):
        self.login(self.eve)
        with fake_ai(reply(rfi()), counts=[3000]) as client:
            self.client.post(self.url('ai_rfi_draft', self.bid.pk), {'revisions': [self.spec.pk]})
            self.switch_on(enabled=False)
            run_worker()
        self.assertEqual((client.requests, client.count_requests), ([], []))
        self.assertEqual(self.rfis(), [])

    def test_bid_no_longer_open_is_refused_at_run_time(self):
        self.login(self.eve)
        with fake_ai(reply(rfi())) as client:
            self.client.post(self.url('ai_rfi_draft', self.bid.pk), {'revisions': [self.spec.pk]})
            with org_context(self.org_a):
                Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_LOST)
            run_worker()
        self.assertEqual(client.requests, [])
        with org_context(self.org_a):
            self.assertEqual(AIRun.objects.get().status, AIRun.FAILED)
            self.assertFalse(AIUsage.objects.exists())


class NothingOutsideUntilIssued(Setup):
    def setUp(self):
        super().setUp()
        self.draft(reply(rfi(subject='Ceiling <script>alert(1)</script> grid', question='<b>Which</b> is right?')))
        [self.d] = self.rfis()

    def externals_see(self, rfi_obj):
        """Every place an external might find an RFI: register, detail, project page, search, Needs me."""
        seen = {}
        detail = self.url('rfi_detail', self.bid.pk, rfi_obj.pk)  # its link (the help panel quotes "RFI-001")
        for user in (self.carla, self.sid):
            self.login(user)
            pages = {'register': self.client.get(self.url('rfi_list', self.bid.pk) + '?status=all'),
                     'draft filter': self.client.get(self.url('rfi_list', self.bid.pk) + '?status=DRAFT'),
                     'detail': self.client.get(self.url('rfi_detail', self.bid.pk, rfi_obj.pk)),
                     'project': self.client.get(self.url('project_detail', self.bid.pk)),
                     'search': self.client.get(self.url('search') + '?q=Ceiling'),
                     'needs me': self.client.get(self.url('needs_me'))}
            for name, r in pages.items():
                body = r.content.decode()
                if r.status_code == 200 and (detail in body or 'Ceiling &lt;script' in body):
                    seen.setdefault(user.email, []).append(name)
        return seen

    def test_a_draft_reaches_nobody_outside_even_if_shared(self):
        self.assertEqual(self.externals_see(self.d), {})
        with org_context(self.org_a):
            self.d.is_shared = True
            self.d.ball_in_court_user = self.carla
            self.d.save(actor=self.eve)
        self.assertEqual(self.externals_see(self.d), {})
        with org_context(self.org_a):
            self.assertFalse(Notification.objects.filter(user=self.carla).exists())

    def test_issued_by_staff_it_follows_the_normal_rules(self):
        with org_context(self.org_a):
            self.d.is_shared = True
            self.d.ball_in_court_user = self.carla
            self.d.save(actor=self.eve)
        self.login(self.carla)  # an external can't issue it
        self.client.post(self.url('rfi_detail', self.bid.pk, self.d.pk), {'action': 'issue'})
        with org_context(self.org_a):
            self.d.refresh_from_db()
        self.assertEqual(self.d.status, RFI.STATUS_DRAFT)
        self.login(self.eve)
        self.client.post(self.url('rfi_detail', self.bid.pk, self.d.pk), {'action': 'issue'})
        with org_context(self.org_a):
            self.d.refresh_from_db()
            self.assertTrue(Notification.objects.filter(user=self.carla).exists())
        self.assertEqual(self.d.status, RFI.STATUS_OPEN)
        self.assertIn('register', self.externals_see(self.d).get(self.carla.email, []))
        self.login(self.carla)
        page = self.client.get(self.url('rfi_detail', self.bid.pk, self.d.pk))
        self.assertNotContains(page, 'ceiling rate on 410')  # why it matters is for staff only
        self.assertNotContains(page, 'AI draft')

    def test_model_text_is_escaped_and_the_basis_is_staff_only(self):
        self.login(self.eve)
        page = self.client.get(self.url('rfi_detail', self.bid.pk, self.d.pk))
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page, '<script>alert(1)')
        self.assertContains(page, '&lt;b&gt;Which&lt;/b&gt;')
        self.assertContains(page, 'ceiling rate on 410')
        self.assertContains(page, 'AI draft')
        brief = self.client.get(self.url('ai_brief', self.bid.pk))
        self.assertContains(brief, self.url('rfi_detail', self.bid.pk, self.d.pk))


class AccessTests(Setup):
    def test_who_may_draft_rfis(self):
        url = self.url('ai_rfi_draft', self.bid.pk)
        expected = {self.alice: 302, self.adam: 302, self.eve: 302, self.pat: 404, self.stan: 404, self.carla: 403,
                    self.connie: 403, self.sid: 403, self.bea: 403, self.nora: 403, self.bob: 404}
        with fake_ai() as client:
            for user, code in expected.items():
                with self.subTest(user=user.email):
                    self.assertEqual(self.status_for(user, url, 'post', {'revisions': [self.spec.pk]}), code)
        self.assertEqual(client.requests, [])  # the page only queues; nothing ran
        with org_context(self.org_a):
            self.assertEqual(AIRun.objects.count(), 1)  # the three allowed share one open run
        self.assertEqual(self.status_for(self.eve, url), 405)
