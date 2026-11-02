"""Help topics (the ? on each page), the help pages, and the getting-started checklist."""
from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse

from changes.tests.base import CommercialTestBase
from costs.access import settings_for
from help import onboarding, topics
from help.models import OnboardingState
from projects.models import Project, ProjectMember
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership, TeamInvite


class MarkdownTests(SimpleTestCase):
    def test_subset(self):
        html = topics.render_markdown(
            '## Raise an RFI\nWrite the **question**, then *send* it.\n\n1. One\n2. Two\n\n- a\n- b\n'
            '[Settings](/{org}/settings/)', 'acme')
        self.assertIn('<h3>Raise an RFI</h3>', html)
        self.assertIn('<strong>question</strong>', html)
        self.assertIn('<em>send</em>', html)
        self.assertIn('<ol><li>One</li><li>Two</li></ol>', html)
        self.assertIn('<ul><li>a</li><li>b</li></ul>', html)
        self.assertIn('<a href="/acme/settings/">Settings</a>', html)

    def test_everything_typed_is_escaped_and_links_are_checked(self):
        html = topics.render_markdown('<script>alert(1)</script> [x](javascript:alert(1)) [y](//evil.example/) '
                                      '[z](https://ok.example/a?b=1&c="2")')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('javascript:', html.replace('x', ''))
        self.assertNotIn('href="//evil', html)
        self.assertIn('href="https://ok.example/a?b=1&amp;c=&quot;2&quot;"', html)


class TopicFileTests(SimpleTestCase):
    def test_every_topic_names_real_pages_and_has_a_body(self):
        from django.urls import get_resolver
        names = {n for n in get_resolver().reverse_dict.keys() if isinstance(n, str)}
        all_topics = topics.topics()
        self.assertGreater(len(all_topics), 25)
        for t in all_topics.values():
            with self.subTest(topic=t.slug):
                self.assertTrue(t.title)
                self.assertTrue(t.pages, 'say which pages the ? appears on')
                self.assertTrue(t.body)
                self.assertLess(len(t.body.split()), 160, 'keep it short: 5 to 10 lines')
                for page in t.pages:
                    self.assertIn(page, names, f'{page} is not a URL name')

    def test_reader_specific_topic_wins(self):
        self.assertEqual(topics.for_page('rfi_list', internal=True).slug, 'rfis')
        self.assertEqual(topics.for_page('rfi_list', internal=False).slug, 'rfis-external')
        self.assertEqual(topics.for_page('needs_me', internal=False).slug, 'needs-me')
        self.assertIsNone(topics.for_page('cost_report', internal=False))  # staff-only page, staff-only help
        self.assertIsNone(topics.for_page('nothing_here', internal=True))

    def test_main_pages_have_help(self):
        for page in ('estimating_home', 'delivery_home', 'needs_me', 'project_list', 'project_detail', 'rfi_list',
                     'variation_list', 'document_list', 'programme', 'issue_list', 'sub_list', 'team', 'cost_report',
                     'application_list', 'subpay_list', 'procurement_schedule', 'submittal_list', 'diary_register',
                     'change_register', 'contract_detail', 'pipeline', 'levelling_index', 'search'):
            with self.subTest(page=page):
                self.assertIsNotNone(topics.for_page(page, internal=True))
        for page in ('dashboard', 'needs_me', 'project_detail', 'project_decisions', 'rfi_list', 'document_list',
                     'programme', 'issue_list', 'tender'):
            with self.subTest(page=page, who='external'):
                self.assertIsNotNone(topics.for_page(page, internal=False))


class HelpPageTests(CommercialTestBase):
    def test_question_mark_opens_the_page_topic(self):
        self.login(self.pat)
        r = self.client.get(self.url('rfi_list', self.pa.pk))
        self.assertContains(r, 'data-help-open="help-panel"')
        self.assertContains(r, 'id="help-panel"')
        self.assertContains(r, 'Requests for information')
        self.assertContains(r, 'data-search-box')

    def test_externals_get_their_own_wording_and_no_staff_topics(self):
        self.login(self.carla)
        r = self.client.get(self.url('rfi_list', self.pa.pk))
        self.assertContains(r, 'Questions between you and the site team')
        self.assertNotContains(r, 'Mark it <em>internal</em>')
        self.assertEqual(self.client.get(self.url('help_topic', 'costs')).status_code, 404)
        self.assertEqual(self.client.get(self.url('help_topic', 'rfis-external')).status_code, 200)
        index = self.client.get(self.url('help_index'))
        self.assertNotContains(index, 'Cost control')
        self.assertContains(index, 'Drawings and documents')

    def test_staff_help_pages(self):
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('help_index')), 'Cost control')
        self.assertContains(self.client.get(self.url('help_topic', 'costs')), 'forecast final cost')
        self.assertEqual(self.client.get(self.url('help_topic', 'no-such-topic')).status_code, 404)

    def test_other_company_gets_404(self):
        self.login(self.bob)
        self.assertEqual(self.client.get(self.url('help_index')).status_code, 404)
        self.assertEqual(self.client.get(self.url('help_topic', 'rfis')).status_code, 404)


class OnboardingTests(CommercialTestBase):
    """Alpha (from the base) has a project, people and subs. A brand-new company has none of it."""

    def setUp(self):
        super().setUp()
        from accounts.views import create_organization
        from core.tests.base import make_user
        self.nell = make_user('nell@new.test', 'Nell New')
        self.org_n = create_organization('Newco Interiors', self.nell)

    def checklist_for(self, user, org):
        self.client.logout()
        self.login(user)
        return self.client.get(reverse('estimating_home', args=[org.slug]))

    def steps(self, org):
        with org_context(org):
            return {s.key: s.done for s in onboarding.steps(org)}

    def test_new_company_owner_sees_all_steps_to_do(self):
        r = self.checklist_for(self.nell, self.org_n)
        self.assertContains(r, 'Getting started')
        self.assertContains(r, '0 of 5 done')
        for label in ('Add your logo and colour', 'Invite your team', 'Set up your cost codes',
                      'Add your first project', 'Invite a subcontractor'):
            self.assertContains(r, label)
        self.assertEqual(self.steps(self.org_n), dict.fromkeys(('brand', 'team', 'codes', 'project', 'sub'), False))

    def test_each_step_ticks_itself(self):
        org = self.org_n
        org.brand_colour = '#1D5D9B'
        org.save()
        self.assertTrue(self.steps(org)['brand'])
        TeamInvite.objects.create(organization=org, email='est@new.test', invited_by=self.nell)
        self.assertTrue(self.steps(org)['team'])
        with org_context(org):
            settings_for(org)
        self.assertTrue(self.steps(org)['codes'])
        with org_context(org):
            p = Project.objects.create(name='First job', stage=Project.STAGE_LIVE)
        self.assertTrue(self.steps(org)['project'])
        self.assertFalse(self.steps(org)['sub'])
        with org_context(org):
            link = next(s for s in onboarding.steps(org) if s.key == 'sub').url
        self.assertEqual(link, reverse('project_team', args=[org.slug, p.pk]))
        Membership.objects.create(organization=org, user=self.sid, role=Membership.ROLE_EXTERNAL)
        with org_context(org):
            ProjectMember.objects.create(project=p, user=self.sid, role=ProjectMember.ROLE_SUBCONTRACTOR)
        self.assertTrue(all(self.steps(org).values()))
        self.assertNotContains(self.checklist_for(self.nell, org), 'onboard-steps')  # all done: gone

    def test_only_owners_and_admins_see_it(self):
        Membership.objects.create(organization=self.org_n, user=self.pat, role=Membership.ROLE_MEMBER)
        r = self.checklist_for(self.pat, self.org_n)
        self.assertNotContains(r, 'Getting started</h2>')
        self.assertContains(r, 'Get set up')  # the plain list staff always had on an empty company

    def test_dismiss(self):
        self.login(self.nell)
        url = reverse('onboarding_dismiss', args=[self.org_n.slug])
        self.assertEqual(self.client.get(url).status_code, 405)
        r = self.client.post(url)
        self.assertEqual(r.status_code, 302)
        self.assertNotContains(self.checklist_for(self.nell, self.org_n), 'onboard-steps')
        with org_context(self.org_n):
            self.assertEqual(OnboardingState.objects.get().dismissed_by, self.nell)

    def test_dismiss_is_for_owners_and_admins_of_that_company(self):
        Membership.objects.create(organization=self.org_n, user=self.pat, role=Membership.ROLE_MEMBER)
        url = reverse('onboarding_dismiss', args=[self.org_n.slug])
        for user, status in ((self.pat, 403), (self.alice, 404)):
            self.client.logout()
            self.login(user)
            self.assertEqual(self.client.post(url).status_code, status)
        with org_context(self.org_n):
            self.assertFalse(OnboardingState.objects.exists())

    def test_seeded_company_with_everything_done_sees_nothing(self):
        Membership.objects.filter(organization=self.org_a, user=self.alice).update(role=Membership.ROLE_OWNER)
        self.org_a.brand_colour = '#123456'
        self.org_a.save()
        with org_context(self.org_a):
            settings_for(self.org_a)
        self.assertNotContains(self.checklist_for(self.alice, self.org_a), 'onboard-steps')

    def test_no_organisation_fails_closed(self):
        from help.models import OnboardingState as S
        with self.assertRaises(TenantContextMissing):
            S.objects.exists()

    def test_dismiss_url_exists(self):
        try:
            reverse('onboarding_dismiss', args=['x'])
        except NoReverseMatch:  # pragma: no cover
            self.fail('onboarding_dismiss is not routed')
