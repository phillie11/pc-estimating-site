"""
"Needs me": the registry, each built-in provider's access rules, the pages, the portal
layout, the dashboards' links and the digest.
"""
import io
from datetime import timedelta
from decimal import Decimal

from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

import actions
from accounts.models import User
from accounts.views import create_organization
from delivery.models import Rams, SubVariation
from directory.models import Package, Subcontractor, Trade
from projects.models import Issue, Project, ProjectMember, Task
from rfis.models import RFI
from tenancy.context import org_context
from tenancy.context import TenantContextMissing
from tenancy.models import ActivityEvent, Membership
from tenders.models import Addendum, AddendumReceipt
from variations.models import Variation

from . import registry
from .models import ProjectVisit

PW = 'Correct-Horse-9'


def make_user(email, name):
    return User.objects.create_user(email=email, password=PW, full_name=name)


def pdf():
    return SimpleUploadedFile('rams.pdf', b'%PDF-1.4\n%%EOF\n', content_type='application/pdf')


class World:
    """One company with a live job, a bid out to tender and an archived job, and every kind of item waiting."""

    def __init__(self, tag, owner):
        self.tag = tag
        self.owner = owner
        self.org = create_organization(f'{tag} Fitout', owner)
        self.pm = make_user(f'pm@{tag}.test', f'{tag} PM')
        self.est = make_user(f'est@{tag}.test', f'{tag} Estimator')
        self.client = make_user(f'client@{tag}.test', f'{tag} Client')
        self.consultant = make_user(f'qs@{tag}.test', f'{tag} Consultant')
        self.sub = make_user(f'sub@{tag}.test', f'{tag} Sub')
        self.bidder = make_user(f'bid@{tag}.test', f'{tag} Bidder')
        today = timezone.localdate()
        with org_context(self.org):
            Membership.objects.create(organization=self.org, user=self.pm, role=Membership.ROLE_MEMBER,
                                      area=Membership.AREA_DELIVERY)
            Membership.objects.create(organization=self.org, user=self.est, role=Membership.ROLE_MEMBER,
                                      area=Membership.AREA_ESTIMATING)
            for u in (self.client, self.consultant, self.sub, self.bidder):
                Membership.objects.create(organization=self.org, user=u, role=Membership.ROLE_EXTERNAL)
            self.job = Project.objects.create(name=f'{tag} live job', stage=Project.STAGE_LIVE, manager=self.pm)
            self.bid = Project.objects.create(name=f'{tag} bid', stage=Project.STAGE_TENDERING, owner=self.est)
            self.old = Project.objects.create(name=f'{tag} archived job', stage=Project.STAGE_LIVE, manager=self.pm)
            self.joinery = Subcontractor.objects.create(company_name=f'{tag} Joinery')
            self.dryline = Subcontractor.objects.create(company_name=f'{tag} Drylining')
            trade, _ = Trade.objects.get_or_create(name='Joinery')
            for p in (self.job, self.old):
                ProjectMember.objects.create(project=p, user=self.client, role=ProjectMember.ROLE_CLIENT)
                ProjectMember.objects.create(project=p, user=self.consultant, role=ProjectMember.ROLE_CONSULTANT)
                ProjectMember.objects.create(project=p, user=self.sub, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                             subcontractor=self.joinery)
                Package.objects.create(project=p, subcontractor=self.joinery, trade=trade, status=Package.APPOINTED,
                                       quote_value=Decimal('10000'))
            ProjectMember.objects.create(project=self.bid, user=self.bidder, role=ProjectMember.ROLE_BIDDER,
                                         subcontractor=self.dryline)
            self.package = Package.objects.create(project=self.bid, subcontractor=self.dryline, trade=trade,
                                                  status=Package.ENQUIRY, quote_due=today + timedelta(days=3))
            self.addendum = Addendum.objects.create(project=self.bid, title=f'{tag} revised layout', description='x')
            self.items = {}
            for p in (self.job, self.old):
                key = 'old' if p == self.old else 'job'
                rfi = RFI(project=p, title=f'{tag} {key} veneer?', question='?', ball_in_court_user=self.client,
                          due_date=today - timedelta(days=1))
                rfi.save(actor=self.pm)
                hidden = RFI(project=p, title=f'{tag} {key} internal rfi', question='?', ball_in_court_user=self.client,
                             is_shared=False)
                hidden.save(actor=self.pm)
                pm_rfi = RFI(project=p, title=f'{tag} {key} rfi for pm', question='?', ball_in_court_user=self.pm,
                             due_date=today)
                pm_rfi.save(actor=self.client)
                vo = Variation.objects.create(project=p, title=f'{tag} {key} extra ceiling', description='x',
                                              value=Decimal('1850.50'), status=Variation.STATUS_SUBMITTED,
                                              submitted_at=timezone.now())
                Variation.objects.create(project=p, title=f'{tag} {key} draft vo', description='x', value=Decimal('9'))
                Rams.objects.create(project=p, subcontractor=self.joinery, title=f'{tag} {key} rams to revise',
                                    file=pdf(), status=Rams.REJECTED, reviewed_at=timezone.now())
                Rams.objects.create(project=p, subcontractor=self.joinery, title=f'{tag} {key} rams to review', file=pdf())
                SubVariation.objects.create(project=p, subcontractor=self.joinery, title=f'{tag} {key} sub claim',
                                            description='x', value=Decimal('640.00'))
                Issue.objects.create(project=p, title=f'{tag} {key} snag for sub', assignee=self.sub, created_by=self.pm,
                                     due_date=today + timedelta(days=2))
                Issue.objects.create(project=p, title=f'{tag} {key} snag ready', assignee=self.sub, created_by=self.pm,
                                     status=Issue.STATUS_READY)
                Issue.objects.create(project=p, title=f'{tag} {key} snag closed', assignee=self.sub,
                                     status=Issue.STATUS_CLOSED)
                Task.objects.create(project=p, title=f'{tag} {key} task for client', assignee=self.client,
                                    due_date=today + timedelta(days=20))
                Task.objects.create(project=p, title=f'{tag} {key} done task', assignee=self.client,
                                    status=Task.STATUS_DONE)
                self.items[key] = {'vo': vo, 'rfi': rfi}
            self.old.archive(owner)

    def membership(self, user):
        return Membership.objects.select_related('organization').get(organization=self.org, user=user)

    def titles(self, user):
        with org_context(self.org):
            return {i.title for i in actions.for_user(actions.request_for(user, self.membership(user)))}


class Base(TestCase):
    def setUp(self):
        self.a = World('Alpha', make_user('owner@alpha.test', 'Alpha Owner'))
        self.b = World('Bravo', make_user('owner@bravo.test', 'Bravo Owner'))

    def login(self, user):
        self.client.force_login(user, backend='django.contrib.auth.backends.ModelBackend')

    def url(self, name, world, *args):
        return reverse(name, args=[world.org.slug, *args])


class RegistryUnitTests(TestCase):
    def test_groups(self):
        today = timezone.localdate()
        g = registry.group_for
        self.assertEqual(g(today - timedelta(days=1), today), 'overdue')
        self.assertEqual(g(today, today), 'today')
        self.assertEqual(g(today + timedelta(days=6), today), 'week')
        self.assertEqual(g(today + timedelta(days=7), today), 'later')
        self.assertEqual(g(None, today), 'later')

    def test_grouped_keeps_order_and_drops_empty_groups(self):
        today = timezone.localdate()
        items = [registry.Item(kind='x', title='b', url='/', due=today), registry.Item(kind='x', title='a', url='/')]
        self.assertEqual([code for code, _, _ in registry.grouped(items)], ['today', 'later'])
        self.assertEqual([code for code, _, _ in registry.grouped([], include_empty=True)],
                         ['overdue', 'today', 'week', 'later'])

    def test_total_is_decimal_and_skips_blanks(self):
        items = [registry.Item(kind='x', title='a', url='/', amount=Decimal('0.10')),
                 registry.Item(kind='x', title='b', url='/', amount=Decimal('0.20')),
                 registry.Item(kind='x', title='c', url='/')]
        self.assertEqual(registry.total(items), Decimal('0.30'))
        self.assertIsInstance(registry.total([]), Decimal)

    def test_register_validates_and_replaces(self):
        with self.assertRaises(TypeError):
            registry.register(object())

        class P:
            kind, label = 'zz_test', 'Test'

            def items(self, request):
                return []
        first, second = P(), P()
        try:
            registry.register(first)
            registry.register(second)
            self.assertIs([p for p in registry.providers() if p.kind == 'zz_test'][0], second)
        finally:
            registry.unregister('zz_test')

    def test_every_built_in_is_registered(self):
        self.assertEqual({k for k, _ in actions.kinds()} >= {
            'record', 'client_variation', 'rams_revise', 'rams_review', 'sub_variation', 'addendum', 'tender',
            'snag', 'snag_inspect', 'task'}, True)

    def test_anonymous_or_no_membership_gets_nothing(self):
        from types import SimpleNamespace

        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(actions.for_user(SimpleNamespace(user=AnonymousUser(), membership=None)), [])


class ProviderAccessTests(Base):
    """Each provider applies the same rules as the page its items link to."""

    def test_client(self):
        t = self.a.titles(self.a.client)
        self.assertIn('Alpha job veneer?', t)                     # shared RFI in their court
        self.assertIn('Alpha job extra ceiling', t)               # variation to approve
        self.assertIn('Alpha job task for client', t)
        for hidden in ('Alpha job internal rfi', 'Alpha job draft vo', 'Alpha job rams to revise',
                       'Alpha job sub claim', 'Alpha job snag for sub', 'Alpha job done task', 'Alpha job rfi for pm'):
            self.assertNotIn(hidden, t)
        self.assertFalse([x for x in t if 'archived' in x or ' old ' in x])

    def test_consultant_sees_rfis_but_does_not_approve_variations(self):
        with org_context(self.a.org):
            r = RFI(project=self.a.job, title='For the consultant', question='?', ball_in_court_user=self.a.consultant)
            r.save(actor=self.a.pm)
        t = self.a.titles(self.a.consultant)
        self.assertIn('For the consultant', t)
        self.assertNotIn('Alpha job extra ceiling', t)

    def test_subcontractor(self):
        t = self.a.titles(self.a.sub)
        self.assertIn('Alpha job rams to revise', t)
        self.assertIn('Alpha job snag for sub', t)
        for hidden in ('Alpha job snag ready', 'Alpha job snag closed', 'Alpha job extra ceiling', 'Alpha job veneer?',
                       'Alpha job sub claim', 'Alpha job rams to review'):
            self.assertNotIn(hidden, t)
        self.assertFalse([x for x in t if ' old ' in x])

    def test_subcontractor_only_sees_their_own_companys_rams(self):
        with org_context(self.a.org):
            other = Subcontractor.objects.create(company_name='Someone else')
            Rams.objects.create(project=self.a.job, subcontractor=other, title='Not yours', file=pdf(),
                                status=Rams.REJECTED)
        self.assertNotIn('Not yours', self.a.titles(self.a.sub))

    def test_tendering_subcontractor(self):
        t = self.a.titles(self.a.bidder)
        self.assertEqual(t, {'Price Joinery', 'Alpha revised layout'})
        with org_context(self.a.org):
            AddendumReceipt.objects.create(addendum=self.a.addendum, subcontractor=self.a.dryline, user=self.a.bidder)
        self.assertEqual(self.a.titles(self.a.bidder), {'Price Joinery'})

    def test_addendum_stops_mattering_once_the_package_is_decided(self):
        with org_context(self.a.org):
            Package.objects.filter(pk=self.a.package.pk).update(status=Package.DECLINED)
        self.assertEqual(self.a.titles(self.a.bidder), set())

    def test_project_manager(self):
        t = self.a.titles(self.a.pm)
        for shown in ('Alpha job rfi for pm', 'Alpha job rams to review', 'Alpha job sub claim', 'Alpha job snag ready'):
            self.assertIn(shown, t)
        for hidden in ('Alpha job rams to revise', 'Alpha job extra ceiling', 'Alpha job snag for sub'):
            self.assertNotIn(hidden, t)
        self.assertFalse([x for x in t if ' old ' in x], 'archived project items must not show')

    def test_staff_not_running_the_job_dont_get_its_rams(self):
        """RAMS and sub variations go to the job's team (delivery.views.site_team), not every member of staff."""
        other = make_user('other@alpha.test', 'Other PM')
        with org_context(self.a.org):
            Membership.objects.create(organization=self.a.org, user=other, role=Membership.ROLE_MEMBER)
        # Staff in both sections get the company-wide tender coverage alert, but nothing from the job
        self.assertEqual(self.a.titles(other), {'Thin quote cover on Alpha bid'})
        with org_context(self.a.org):
            ProjectMember.objects.create(project=self.a.job, user=other, role=ProjectMember.ROLE_SITE)
        self.assertIn('Alpha job rams to review', self.a.titles(other))

    def test_bid_lead_gets_rams_when_a_job_has_no_team(self):
        with org_context(self.a.org):
            Project.objects.filter(pk=self.a.job.pk).update(manager=None, owner=self.a.owner)
        self.assertIn('Alpha job rams to review', self.a.titles(self.a.owner))

    def test_estimator_sees_nothing_from_live_jobs(self):
        with org_context(self.a.org):
            r = RFI(project=self.a.job, title='Live job rfi for estimator', question='?', ball_in_court_user=self.a.est)
            r.save(actor=self.a.pm)
            Task.objects.create(project=self.a.job, title='Live job task for estimator', assignee=self.a.est)
            Task.objects.create(project=self.a.bid, title='Bid task for estimator', assignee=self.a.est)
        # Their bid's task and its quote-cover alert; nothing from the live job
        self.assertEqual(self.a.titles(self.a.est), {'Bid task for estimator', 'Thin quote cover on Alpha bid'})

    def test_removed_from_project_loses_its_items(self):
        with org_context(self.a.org):
            ProjectMember.objects.filter(project=self.a.job, user=self.a.client).delete()
        self.assertEqual(self.a.titles(self.a.client), set())

    def test_items_carry_url_ref_project_due_and_money(self):
        with org_context(self.a.org):
            items = actions.for_user(actions.request_for(self.a.client, self.a.membership(self.a.client)))
        vo = next(i for i in items if i.kind == 'client_variation')
        self.assertEqual(vo.ref, 'VO-001')
        self.assertEqual(vo.amount, Decimal('1850.50'))
        self.assertEqual(vo.project, self.a.job)
        self.assertEqual(vo.url, self.a.items['job']['vo'].get_absolute_url())
        self.assertEqual(items[0].kind, 'rfi')  # overdue first
        self.assertTrue(items[0].is_overdue)
        self.assertEqual(actions.total(items), Decimal('1850.50'))


class CrossProviderTenancyTests(Base):
    """Every provider stays inside the current company, and fails closed without one."""

    def test_each_company_sees_only_its_own_items(self):
        for world, other in ((self.a, self.b), (self.b, self.a)):
            for user in (world.owner, world.pm, world.est, world.client, world.consultant, world.sub, world.bidder):
                titles = world.titles(user)
                self.assertFalse([t for t in titles if other.tag in t], f'{user} saw {other.tag} items')

    def test_someone_in_both_companies_sees_only_the_current_one(self):
        both = self.a.client
        with org_context(self.b.org):
            Membership.objects.create(organization=self.b.org, user=both, role=Membership.ROLE_EXTERNAL)
            ProjectMember.objects.create(project=self.b.job, user=both, role=ProjectMember.ROLE_CLIENT)
        in_a = self.a.titles(both)
        with org_context(self.b.org):
            in_b = {i.title for i in actions.for_user(actions.request_for(both, self.b.membership(both)))}
        self.assertIn('Alpha job extra ceiling', in_a)
        self.assertIn('Bravo job extra ceiling', in_b)
        self.assertFalse([t for t in in_a if 'Bravo' in t])
        self.assertFalse([t for t in in_b if 'Alpha' in t])

    def test_every_provider_fails_closed_without_an_organisation(self):
        users = (self.a.owner, self.a.pm, self.a.client, self.a.sub, self.a.bidder)
        for provider in registry.providers():
            for user in users:
                req = actions.request_for(user, self.a.membership(user))
                try:
                    items = list(provider.items(req))
                except TenantContextMissing:
                    continue
                self.assertEqual(items, [], f'{provider.kind} returned rows with no organisation for {user}')

    def test_needs_me_page_of_another_company_is_a_404(self):
        self.login(self.b.client)
        self.assertEqual(self.client.get(self.url('needs_me', self.a)).status_code, 404)
        self.assertEqual(self.client.get(self.url('project_decisions', self.a, self.a.job.pk)).status_code, 404)
        self.login(self.b.pm)
        self.assertEqual(self.client.get(self.url('project_decisions', self.b, self.a.job.pk)).status_code, 404)


class NeedsMePageTests(Base):
    def test_each_role(self):
        expect = {
            # Owners and estimators all get the bid's coverage alert (company-wide, by owner decision)
            self.a.owner: (200, 'Thin quote cover on Alpha bid'), self.a.pm: (200, 'Alpha job sub claim'),
            self.a.est: (200, 'Thin quote cover on Alpha bid'),
            self.a.client: (200, 'Alpha job extra ceiling'), self.a.consultant: (200, None),
            self.a.sub: (200, 'Alpha job rams to revise'), self.a.bidder: (200, 'Alpha revised layout'),
        }
        for user, (code, text) in expect.items():
            self.login(user)
            r = self.client.get(self.url('needs_me', self.a))
            self.assertEqual(r.status_code, code, user)
            if text:
                self.assertContains(r, text)
                self.assertNotContains(r, 'archived job')
            else:
                self.assertContains(r, 'Nothing is waiting on you')
        outsider = make_user('nobody@x.test', 'Nobody')
        self.login(outsider)
        self.assertEqual(self.client.get(self.url('needs_me', self.a)).status_code, 404)

    def test_grouped_and_filtered(self):
        self.login(self.a.client)
        r = self.client.get(self.url('needs_me', self.a))
        self.assertEqual([g[0] for g in r.context['groups']], ['overdue', 'later'])
        self.assertContains(r, '£1,850.50')  # Value waiting on your decision
        r = self.client.get(self.url('needs_me', self.a) + '?kind=client_variation')
        self.assertEqual([i.kind for i in r.context['items']], ['client_variation'])

    def test_bell_badge_counts_needs_me(self):
        self.login(self.a.client)
        r = self.client.get(self.url('notifications', self.a))
        self.assertContains(r, 'Needs me: 3 waiting')
        self.login(self.a.pm)
        r = self.client.get(self.url('delivery_home', self.a))
        self.assertContains(r, 'Needs me: 4 waiting')


class PortalLayoutTests(Base):
    def test_externals_get_the_portal_layout_and_staff_the_sidebar(self):
        self.login(self.a.client)
        r = self.client.get(self.url('project_detail', self.a, self.a.job.pk))
        self.assertContains(r, 'class="pbar"')
        self.assertNotContains(r, '<aside class="side">')
        self.assertContains(r, 'aria-label="Switch project"')
        self.login(self.a.pm)
        r = self.client.get(self.url('project_detail', self.a, self.a.job.pk))
        self.assertContains(r, '<aside class="side">')
        self.assertNotContains(r, 'class="pbar"')

    def test_external_tabs_are_at_most_five(self):
        for user in (self.a.client, self.a.consultant, self.a.sub):
            self.login(user)
            r = self.client.get(self.url('project_detail', self.a, self.a.job.pk))
            labels = [t[1] for t in r.context['tab_row']]
            self.assertLessEqual(len(labels), 5, labels)
            self.assertEqual(labels, ['Overview', 'Decisions', 'Documents', 'Programme', 'Snags'], user)

    def test_subcontractor_sees_your_variations_and_your_rams(self):
        self.login(self.a.sub)
        r = self.client.get(self.url('subvariation_list', self.a, self.a.job.pk))
        self.assertEqual(r.context['tab_group'], 'decisions')
        labels = [t[1] for t in r.context['subtabs']]
        self.assertIn('Your variations', labels)
        self.assertIn('Your RAMS', labels)
        self.assertNotIn('Variations', labels)
        self.login(self.a.client)
        r = self.client.get(self.url('variation_list', self.a, self.a.job.pk))
        labels = [t[1] for t in r.context['subtabs']]
        self.assertIn('Variations', labels)
        self.assertNotIn('Your RAMS', labels)

    def test_decisions_tab(self):
        self.login(self.a.client)
        r = self.client.get(self.url('project_decisions', self.a, self.a.job.pk))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Alpha job extra ceiling')
        self.assertContains(r, 'Approve or reject')
        self.assertContains(r, '£1,850.50')
        self.assertNotContains(r, 'Alpha job rams to revise')
        self.login(self.a.bidder)  # a tendering sub only ever sees their tender
        self.assertEqual(self.client.get(self.url('project_decisions', self.a, self.a.bid.pk)).status_code, 403)
        self.login(self.a.sub)  # not on the bid
        self.assertEqual(self.client.get(self.url('project_decisions', self.a, self.a.bid.pk)).status_code, 404)

    def test_bidder_keeps_a_single_tender_tab(self):
        self.login(self.a.bidder)
        r = self.client.get(self.url('tender', self.a, self.a.bid.pk))
        self.assertEqual([t[0] for t in r.context['tab_row']], ['tender'])

    def test_whats_changed_since_last_visit(self):
        self.login(self.a.client)
        url = self.url('project_detail', self.a, self.a.job.pk)
        r = self.client.get(url)
        self.assertNotContains(r, "What's changed since your last visit")  # first visit
        with org_context(self.a.org):
            visit = ProjectVisit.objects.get(member__user=self.a.client, member__project=self.a.job)
            ProjectVisit.objects.filter(pk=visit.pk).update(last_seen=timezone.now() - timedelta(hours=3))
            ActivityEvent.objects.create(actor=self.a.pm, project=self.a.job, verb='issued', target='GA drawing rev C',
                                         internal_only=False)
            ActivityEvent.objects.create(actor=self.a.pm, project=self.a.job, verb='priced', target='Internal cost plan',
                                         internal_only=True)
        self.login(self.a.client)
        r = self.client.get(url)
        self.assertContains(r, "What's changed since your last visit")
        self.assertContains(r, 'GA drawing rev C')
        self.assertNotContains(r, 'Internal cost plan')
        with org_context(self.a.org):
            visit.refresh_from_db()
        self.assertIsNotNone(visit.previous_seen)

    def test_dashboard_counts_unseen_updates(self):
        self.login(self.a.client)
        self.client.get(self.url('project_detail', self.a, self.a.job.pk))
        with org_context(self.a.org):
            ProjectVisit.objects.update(last_seen=timezone.now() - timedelta(hours=3))
            ActivityEvent.objects.create(actor=self.a.pm, project=self.a.job, verb='issued', target='x', internal_only=False)
        r = self.client.get(self.url('dashboard', self.a))
        self.assertContains(r, '1 update since your last visit')

    def test_staff_are_not_tracked(self):
        self.login(self.a.pm)
        self.client.get(self.url('project_detail', self.a, self.a.job.pk))
        with org_context(self.a.org):
            self.assertFalse(ProjectVisit.objects.exists())


class StaffTabTests(Base):
    def test_live_job_row_is_capped_with_more_menu(self):
        self.login(self.a.owner)
        r = self.client.get(self.url('project_detail', self.a, self.a.job.pk))
        row, more = r.context['tab_row'], r.context['tab_more']
        self.assertLessEqual(len(row) + (1 if more else 0), 8)
        self.assertTrue(more)
        codes = [t[0] for t in r.context['tabs']]
        self.assertIn('rams', codes)
        self.assertIn('subvariations', codes)
        self.assertEqual({t[0] for t in row} | {t[0] for t in more}, set(codes))
        self.assertContains(r, 'class="tabmore')
        self.assertContains(r, 'data-tabbar')
        labels = [t[1] for t in r.context['tabs']]
        self.assertIn('Variations to client', labels)
        self.assertIn('Variations from subs', labels)
        self.assertNotIn('Client variations', labels)

    def test_tab_in_overflow_shows_as_current_on_more(self):
        self.login(self.a.owner)
        r = self.client.get(self.url('task_list', self.a, self.a.job.pk))
        self.assertEqual(r.context['more_on'][0], 'tasks')

    def test_bid_tabs_unchanged(self):
        self.login(self.a.est)
        r = self.client.get(self.url('project_detail', self.a, self.a.bid.pk))
        self.assertEqual([t[0] for t in r.context['tab_row']], ['overview', 'tender', 'packages', 'documents', 'team'])
        self.assertEqual(r.context['tab_more'], [])


class DashboardTests(Base):
    def test_delivery_numbers_link_to_filtered_lists(self):
        self.login(self.a.pm)
        r = self.client.get(self.url('delivery_home', self.a))
        jobs = self.url('project_list', self.a)
        for q in ('?view=live&amp;attention=rams', '?view=live&amp;attention=subvars', '?view=live&amp;attention=client_vos',
                  '?view=live&amp;attention=appoint', '?view=live&amp;mine=1'):
            self.assertContains(r, f'href="{jobs}{q}"')
        self.assertContains(r, 'status=open&amp;scope=live')
        self.assertNotContains(r, 'Morning,')
        self.assertContains(r, 'Alpha job sub claim')  # On-your-plate card now reads the registry

    def test_estimating_numbers_link_to_filtered_lists(self):
        self.login(self.a.est)
        r = self.client.get(self.url('estimating_home', self.a))
        bids = self.url('bid_list', self.a)
        for q in ('?view=open', '?view=open&amp;due=14', '?view=open&amp;attention=quotes',
                  '?view=open&amp;attention=queries'):
            self.assertContains(r, f'href="{bids}{q}"')
        self.assertContains(r, self.url('tender_analytics', self.a))

    def test_filtered_lists_match_the_numbers(self):
        self.login(self.a.pm)
        kpi = self.client.get(self.url('delivery_home', self.a)).context['kpi']
        jobs = self.url('project_list', self.a)
        r = self.client.get(jobs + '?view=live&attention=rams')
        self.assertEqual(list(r.context['projects']), [self.a.job])
        self.assertEqual(kpi['rams'], 1)
        self.assertContains(r, 'with RAMS waiting for review')
        with org_context(self.a.org):
            Rams.objects.filter(project=self.a.job).update(status=Rams.APPROVED)
        r = self.client.get(jobs + '?view=live&attention=rams')
        self.assertEqual(list(r.context['projects']), [])
        r = self.client.get(jobs + '?view=live&mine=1')
        self.assertEqual(list(r.context['projects']), [self.a.job])
        self.login(self.a.est)
        r = self.client.get(self.url('bid_list', self.a) + '?view=open&attention=quotes')
        self.assertEqual(list(r.context['projects']), [self.a.bid])
        r = self.client.get(self.url('bid_list', self.a) + '?view=open&attention=quotes_overdue')
        self.assertEqual(list(r.context['projects']), [])

    def test_rfi_register_filters(self):
        self.login(self.a.pm)
        r = self.client.get(self.url('rfi_register', self.a) + '?status=open&scope=live&overdue=1')
        self.assertEqual([x.title for x in r.context['rfis']], ['Alpha job veneer?'])
        self.assertContains(r, 'past their due date')

    def test_client_dashboard_leads_with_decisions_and_money(self):
        self.login(self.a.client)
        r = self.client.get(self.url('dashboard', self.a))
        html = r.content.decode()
        self.assertLess(html.index('id="dw"'), html.index('<h2>Your projects</h2>'))
        self.assertContains(r, 'Variations waiting for your approval')
        self.assertContains(r, '£1,850.50')
        self.assertContains(r, 'Alpha job extra ceiling')
        self.assertEqual(r.context['my_rfis'][0].title, 'Alpha job veneer?')  # older context kept for callers

    def test_sub_dashboard_shows_decisions_without_client_money(self):
        self.login(self.a.sub)
        r = self.client.get(self.url('dashboard', self.a))
        self.assertContains(r, 'Alpha job rams to revise')
        self.assertNotContains(r, 'Variations waiting for your approval')


class UiKitTests(Base):
    def test_staff_only(self):
        self.login(self.a.pm)
        r = self.client.get(self.url('ui_kit', self.a))
        self.assertEqual(r.status_code, 200)
        for part in ('status_badge', 'empty_state', 'register_table', 'decision_panel'):
            self.assertContains(r, part)
        for user in (self.a.client, self.a.sub, self.a.bidder):
            self.login(user)
            self.assertEqual(self.client.get(self.url('ui_kit', self.a)).status_code, 403)

    def test_status_badge_tag(self):
        from django.template import Context, Template
        t = Template('{% load ui %}{% status_badge vo %}|{% status_badge "Overdue" "bad" %}|{% status_badge "x" "evil" %}')
        out = t.render(Context({'vo': Variation(status=Variation.STATUS_APPROVED)}))
        self.assertEqual(out, '<span class="badge good">Approved</span>|<span class="badge bad">Overdue</span>|'
                              '<span class="badge muted">x</span>')

    def test_money_short_is_decimal_and_rounds_half_up(self):
        from tenancy.templatetags.ui import money_short
        self.assertEqual(money_short(Decimal('2500'), '£'), '£3k')
        self.assertEqual(money_short(Decimal('1250000'), '£'), '£1.3m')
        self.assertEqual(money_short(None), '—')


@override_settings(SITE_URL='https://portal.test')
class DigestTests(Base):
    def run_digest(self, today=None):
        mail.outbox.clear()
        if today is None:
            call_command('send_reminders', stdout=io.StringIO())
        else:
            with mock.patch('django.utils.timezone.localdate', return_value=today):
                call_command('send_reminders', stdout=io.StringIO())
        return {m.to[0]: m for m in mail.outbox}

    def test_digest_lists_needs_me_items_with_links(self):
        by = self.run_digest()
        client = by['client@alpha.test'].body
        self.assertIn('Overdue', client)
        self.assertIn('Alpha job veneer?', client)
        self.assertIn('Alpha job extra ceiling', client)  # undated, listed because the email goes anyway
        self.assertIn('£1,850.50', client)
        self.assertIn('https://portal.test' + self.a.items['job']['vo'].get_absolute_url(), client)
        self.assertNotIn('archived', client)
        self.assertNotIn('Bravo', client)
        sub = by['sub@alpha.test'].body
        self.assertIn('Alpha job snag for sub', sub)       # due in 2 days: the reason it was sent
        self.assertIn('Alpha job rams to revise', sub)
        self.assertNotIn('extra ceiling', sub)
        self.assertIn('Alpha revised layout', by['bid@alpha.test'].body)  # quote due in 3 days sends it
        pm = by['pm@alpha.test'].body
        self.assertIn('Alpha job sub claim', pm)
        self.assertNotIn('Tender returns', pm)  # projects-only staff aren't sent bid deadlines

    def test_final_link_printed_once(self):
        body = self.run_digest()['client@alpha.test'].body
        self.assertEqual(body.count('Everything waiting on you:'), 1)
        self.assertEqual(body.count('https://portal.test' + self.url('needs_me', self.a)), 1)

    def test_undated_or_distant_items_never_send_it_on_their_own(self):
        with org_context(self.a.org):
            RFI.objects.filter(project=self.a.job).update(due_date=None)
            from core.models import index_record
            for r in RFI.objects.filter(project=self.a.job):
                index_record(r)
        by = self.run_digest()
        # The client is left with an undated variation and a task due in 20 days
        self.assertNotIn('client@alpha.test', by)
        self.assertNotIn('qs@alpha.test', by)

    def test_extra_items_are_capped(self):
        from . import digest
        with org_context(self.a.org):
            for n in range(digest.EXTRA_CAP + 3):
                Task.objects.create(project=self.a.job, title=f'Undated task {n}', assignee=self.a.client)
        body = self.run_digest()['client@alpha.test'].body
        self.assertEqual(body.count('Undated task'), digest.EXTRA_CAP - 1)  # the variation takes one slot
        self.assertIn('...and', body)

    def test_variation_respond_by_date_is_read_when_the_field_exists(self):
        with org_context(self.a.org):
            vo = self.a.items['job']['vo']
            vo.due_date = timezone.localdate() + timedelta(days=1)  # what the variations branch adds
            from .providers import ClientVariationProvider
            from django.db.models.query import QuerySet
            real = QuerySet.__iter__

            def with_due(qs):
                for obj in real(qs):
                    if isinstance(obj, Variation):
                        obj.due_date = vo.due_date
                    yield obj
            with mock.patch.object(QuerySet, '__iter__', with_due):
                items = ClientVariationProvider().items(
                    actions.request_for(self.a.client, self.a.membership(self.a.client)))
        self.assertEqual([i.due for i in items], [vo.due_date])

    def test_weekly_goes_on_mondays_only_and_off_stops_it(self):
        from .models import DigestPreference
        DigestPreference.set_for(self.a.client, DigestPreference.WEEKLY)
        DigestPreference.set_for(self.a.sub, DigestPreference.OFF)
        today = timezone.localdate()
        monday = today - timedelta(days=today.weekday())
        tuesday = monday + timedelta(days=1)
        self.assertNotIn('client@alpha.test', self.run_digest(tuesday))
        by = self.run_digest(monday)
        self.assertIn('client@alpha.test', by)
        self.assertIn('weekly', by['client@alpha.test'].subject)
        self.assertNotIn('sub@alpha.test', by)
        self.assertNotIn('sub@alpha.test', self.run_digest(tuesday))
        self.assertIn('pm@alpha.test', self.run_digest(tuesday))  # no row: daily

    def test_unsubscribe_link_works_without_login(self):
        from . import digest
        from .models import DigestPreference
        body = self.run_digest()['client@alpha.test'].body
        link = digest.unsubscribe_path(self.a.client)
        self.assertIn('https://portal.test' + link, body)
        self.client.logout()
        r = self.client.get(link)
        self.assertContains(r, 'Stop round-up emails')
        self.assertEqual(DigestPreference.frequency_for(self.a.client), DigestPreference.DAILY)  # a GET changes nothing
        r = self.client.post(link, {'frequency': 'OFF'})
        self.assertContains(r, 'Saved')
        self.assertEqual(DigestPreference.frequency_for(self.a.client), DigestPreference.OFF)
        self.assertNotIn('client@alpha.test', self.run_digest())
        self.client.post(link, {'frequency': 'WEEKLY'})
        self.assertEqual(DigestPreference.frequency_for(self.a.client), DigestPreference.WEEKLY)

    def test_tampered_unsubscribe_token_is_refused(self):
        from . import digest
        token = digest.unsubscribe_token(self.a.client)
        forged = token[:-2] + ('aa' if not token.endswith('aa') else 'bb')
        r = self.client.post(reverse('digest_unsubscribe', args=[forged]), {'frequency': 'OFF'})
        self.assertEqual(r.status_code, 404)
        from .models import DigestPreference
        self.assertFalse(DigestPreference.objects.exists())
        # A token for one person can't be turned into another's
        self.assertNotEqual(digest.unsubscribe_token(self.a.client), digest.unsubscribe_token(self.a.sub))

    def test_preference_on_profile_page_for_staff_and_externals(self):
        from .models import DigestPreference
        for user in (self.a.pm, self.a.client):
            self.login(user)
            r = self.client.get(reverse('profile'))
            self.assertContains(r, 'Round-up email')
            self.assertContains(r, 'value="DAILY" checked')
            self.client.post(reverse('digest_preference'), {'frequency': 'WEEKLY'})
            self.assertEqual(DigestPreference.frequency_for(user), DigestPreference.WEEKLY)
            self.client.post(reverse('digest_preference'), {'frequency': 'NONSENSE'})
            self.assertEqual(DigestPreference.frequency_for(user), DigestPreference.WEEKLY)
        self.client.logout()
        r = self.client.post(reverse('digest_preference'), {'frequency': 'OFF'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r['Location'])


class ReviewFixTests(Base):
    """Regression tests for the independent review of PR #15."""

    def test_non_ascii_digit_project_filter_is_ignored_not_a_500(self):
        self.login(self.a.client)
        r = self.client.get(self.url('needs_me', self.a) + '?project=%C2%B2')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context['items']), 3)

    def test_bidder_switcher_links_tenders_to_the_tender_page(self):
        from tenders.services import tender_url
        self.login(self.a.bidder)
        r = self.client.get(self.url('dashboard', self.a))
        switcher = r.context['portal_switcher']
        switcher = switcher() if callable(switcher) else switcher
        self.assertEqual([(e['name'], e['url']) for e in switcher], [(self.a.bid.name, tender_url(self.a.bid))])
        self.assertNotContains(r, f'href="{self.a.bid.get_absolute_url()}"')
        self.login(self.a.client)
        r = self.client.get(self.url('dashboard', self.a))
        switcher = r.context['portal_switcher']
        switcher = switcher() if callable(switcher) else switcher
        self.assertEqual([e['url'] for e in switcher], [self.a.job.get_absolute_url()])

    def test_no_silent_cap_and_needs_me_is_paginated(self):
        from . import views
        with org_context(self.a.org):
            for n in range(205):
                r = RFI(project=self.a.job, title=f'Bulk RFI {n}', question='?', ball_in_court_user=self.a.client)
                r.save(actor=self.a.pm)
        self.assertEqual(len([t for t in self.a.titles(self.a.client) if t.startswith('Bulk RFI')]), 205)
        self.login(self.a.client)
        r = self.client.get(self.url('needs_me', self.a))
        self.assertEqual(len(r.context['items']), views.PER_PAGE)
        self.assertEqual(r.context['shown_count'], 208)
        self.assertContains(r, 'aria-label="Needs me: 208 waiting"')
        self.assertContains(r, '<span class="n">99+</span>')
        last = self.client.get(self.url('needs_me', self.a) + '?page=5')
        self.assertEqual(len(last.context['items']), 208 - 4 * views.PER_PAGE)

    def test_badge_count_filter(self):
        from tenancy.templatetags.ui import badge_count
        self.assertEqual([badge_count(0), badge_count(99), badge_count(100), badge_count('x')], ['0', '99', '99+', ''])

    def test_amounts_to_approve_show_pence(self):
        from tenancy.templatetags.ui import money_exact
        self.assertEqual(money_exact(Decimal('1850.5'), '£'), '£1,850.50')
        self.assertEqual(money_exact(Decimal('0.005'), '£'), '£0.01')
        self.assertEqual(money_exact(None), '—')
        self.login(self.a.client)
        for name, args in (('dashboard', []), ('project_decisions', [self.a.job.pk]), ('needs_me', [])):
            r = self.client.get(self.url(name, self.a, *args))
            self.assertContains(r, '£1,850.50')
        self.login(self.a.pm)
        r = self.client.get(self.url('delivery_home', self.a))
        self.assertContains(r, 'Sub variations to decide')
        self.assertNotContains(r, '£640.00</div>')  # dashboard tiles stay short


class HookProviderTests(Base):
    """Hooks into apps on other branches list nothing until the module exists, then plug straight in."""

    def test_missing_modules_list_nothing(self):
        from .providers import HookProvider
        kinds = {p.kind for p in registry.providers()}
        self.assertTrue({'coverage_alert', 'change', 'transmittal'} <= kinds)
        for path, takes in (('zz_not_merged_yet.actions.items', 'request'), ('tenders.zz_not_here.alerts', 'request'),
                            ('zz_not_merged_yet.models.pending', 'user'), ('tenders.actions.zz_no_such_function', 'request')):
            hook = HookProvider('zz', 'Test', path, takes=takes)
            for user in (self.a.owner, self.a.client):
                with org_context(self.a.org):
                    self.assertEqual(hook.items(actions.request_for(user, self.a.membership(user))), [], path)

    def test_dicts_with_extra_keys_keep_only_item_fields_and_the_providers_kind(self):
        from .providers import HookProvider
        item = HookProvider('zz', 'Test', 'x.y')._coerce(
            {'kind': 'tenders.thin_coverage', 'title': 'T', 'url': '/t', 'owner': self.a.est, 'trades': ['Joinery']})
        self.assertEqual((item.kind, item.title, item.url), ('zz', 'T', '/t'))

    def test_a_hook_that_exists_is_used_and_access_filtered(self):
        import sys
        import types

        from .providers import HookProvider
        fake = types.ModuleType('zz_fake_hook')
        a = self.a

        def items(request):
            return [{'title': 'Coverage: 1 bid on drylining', 'url': '/x', 'project': a.bid},
                    {'title': 'Not yours', 'url': '/y', 'project': a.job}]
        fake.items = items
        row = a.items['job']['vo']
        row.title = 'Transmittal T-001'
        fake.pending = lambda user: [row]
        sys.modules['zz_fake_hook'] = fake
        try:
            hook = HookProvider('zz', 'Test', 'zz_fake_hook.items', staff=True)
            with org_context(a.org):
                got = [i.title for i in hook.items(actions.request_for(a.est, a.membership(a.est)))]
            self.assertEqual(got, ['Coverage: 1 bid on drylining'])  # the live job is outside the estimator's sections
            ext = HookProvider('zz2', 'Test', 'zz_fake_hook.pending', takes='user', staff=False)
            with org_context(a.org):
                got = ext.items(actions.request_for(a.client, a.membership(a.client)))
                self.assertEqual([i.title for i in got], ['Transmittal T-001'])
                self.assertEqual(ext.items(actions.request_for(a.owner, a.membership(a.owner))), [])
        finally:
            del sys.modules['zz_fake_hook']

    def test_a_broken_import_inside_an_existing_module_is_not_hidden(self):
        from .providers import HookProvider
        hook = HookProvider('zz', 'Test', 'actions.zz_missing_submodule.items')
        self.assertIsNone(hook.resolve())  # the submodule itself is missing: fine
        import sys
        import types
        broken = types.ModuleType('zz_broken')
        sys.modules['zz_broken'] = broken
        try:
            self.assertIsNone(HookProvider('zz', 'Test', 'zz_broken.nothing_here').resolve())
        finally:
            del sys.modules['zz_broken']


class NotificationLinkTests(Base):
    def test_task_alert_links_to_the_task(self):
        self.login(self.a.pm)
        mail.outbox.clear()
        self.client.post(self.url('task_list', self.a, self.a.job.pk), {
            'title': 'Send O&M manuals', 'assignee': self.a.client.pk, 'priority': 'NORMAL', 'status': 'TODO'})
        with org_context(self.a.org):
            t = Task.objects.get(title='Send O&M manuals')
        self.assertTrue(mail.outbox)
        self.assertIn(self.url('task_list', self.a, self.a.job.pk) + f'#task-{t.pk}', mail.outbox[-1].body)


class SeedDemoTests(TestCase):
    def test_demo_logins_have_something_waiting(self):
        call_command('seed_demo', stdout=io.StringIO())
        from tenancy.models import Organization
        org = Organization.objects.get(slug='northbank-interiors')
        with org_context(org):
            def kinds(email):
                user = User.objects.get(email=email)
                m = Membership.objects.get(organization=org, user=user)
                return {i.kind for i in actions.for_user(actions.request_for(user, m))}
            self.assertIn('client_variation', kinds('client@demo.test'))
            self.assertIn('rfi', kinds('client@demo.test'))
            self.assertIn('tender', kinds('sub@demo.test'))
            self.assertIn('rams_review', kinds('pm@demo.test'))
            self.assertIn('sub_variation', kinds('pm@demo.test'))
            self.assertIn('task', kinds('estimator@demo.test'))
            self.assertIn('task', kinds('owner@demo.test'))
            self.assertIn('addendum', kinds('sub@demo.test'))


class LiveHookTests(Base):
    """The merged hooks: tender coverage alerts and transmittals to acknowledge."""

    def items_of(self, world, user, kind):
        with org_context(world.org):
            return [i for i in actions.for_user(actions.request_for(user, world.membership(user))) if i.kind == kind]

    def test_coverage_alerts_go_to_every_estimator_and_nobody_else(self):
        late_staff = make_user('both@alpha.test', 'Both Sections')
        with org_context(self.a.org):
            Membership.objects.create(organization=self.a.org, user=late_staff, role=Membership.ROLE_MEMBER)
        for user in (self.a.owner, self.a.est, late_staff):  # company-wide: not only the bid lead
            alerts = self.items_of(self.a, user, 'coverage_alert')
            self.assertEqual([i.title for i in alerts], ['Thin quote cover on Alpha bid'], user)
            self.assertEqual(alerts[0].url, reverse('tender', args=[self.a.org.slug, self.a.bid.pk]) + '#coverage')
            self.assertEqual(alerts[0].due, self.a.package.quote_due)
            self.assertIn('Joinery', alerts[0].detail)
        for user in (self.a.pm, self.a.client, self.a.consultant, self.a.sub, self.a.bidder):
            self.assertEqual(self.items_of(self.a, user, 'coverage_alert'), [], user)
        for user in (self.a.owner, self.a.est):
            self.assertFalse([i for i in self.items_of(self.a, user, 'coverage_alert') if 'Bravo' in i.title])

    def test_bidder_never_sees_coverage_on_their_own_tender_page_list(self):
        self.login(self.a.bidder)
        r = self.client.get(self.url('needs_me', self.a))
        self.assertNotContains(r, 'Thin quote cover')
        self.login(self.a.est)
        self.assertContains(self.client.get(self.url('needs_me', self.a)), 'Thin quote cover on Alpha bid')
        self.login(self.b.est)
        self.assertNotContains(self.client.get(self.url('needs_me', self.b)), 'Alpha bid')

    def issue(self, world, users=(), companies=(), due=None, title='Joinery drawings for construction'):
        from documents import services
        from documents.models import Transmittal, check_number
        from projects.models import Document, DocumentRevision
        with org_context(world.org):
            number = 'SLN-ABC-ZZ-02-DR-A-1001'
            d = Document(project=world.job, title='GA plan', created_by=world.pm)
            d.set_number(number, check_number(world.job, number)[1])
            d.save()
            rev = d.add_revision(DocumentRevision(revision='P01', file=pdf(), original_name='P01.pdf', size=10,
                                                  uploaded_by=world.pm))
            t = Transmittal(project=world.job, title=title, purpose=Transmittal.PURPOSE_CONSTRUCTION, due_date=due)
            t.save(actor=world.pm)
            services.set_items(t, [rev])
            services.set_recipients(t, list(users), list(companies))
            services.issue(world.pm, t)
            return Transmittal.objects.get(pk=t.pk)

    def test_transmittals_appear_for_their_recipients_once_with_ref_url_and_due(self):
        due = timezone.localdate() + timedelta(days=2)
        t = self.issue(self.a, users=[self.a.client], companies=[self.a.joinery], due=due)
        for user in (self.a.client, self.a.sub):
            got = self.items_of(self.a, user, 'transmittal')
            self.assertEqual(len(got), 1, user)
            item = got[0]
            self.assertEqual((item.title, item.ref, item.url, item.due, item.project),
                             (t.title, 'TR-001', t.get_absolute_url(), due, self.a.job))
            self.assertEqual(item.status_label, 'To acknowledge')
            # Not listed a second time from the record index (the client is the ball in court)
            with org_context(self.a.org):
                every = actions.for_user(actions.request_for(user, self.a.membership(user)))
            self.assertEqual(len([i for i in every if i.ref == 'TR-001']), 1)
        for user in (self.a.consultant, self.a.bidder, self.a.est, self.a.owner):
            self.assertEqual(self.items_of(self.a, user, 'transmittal'), [], user)
        self.assertEqual(self.items_of(self.b, self.b.client, 'transmittal'), [])
        self.login(self.b.client)
        self.assertNotContains(self.client.get(self.url('needs_me', self.b)), 'Joinery drawings')

    def test_staff_recipients_get_them_too_and_acknowledging_clears_it(self):
        from documents import services
        t = self.issue(self.a, users=[self.a.pm])
        self.assertEqual([i.ref for i in self.items_of(self.a, self.a.pm, 'transmittal')], ['TR-001'])
        with org_context(self.a.org):
            services.acknowledge(self.a.pm, t)
        self.assertEqual(self.items_of(self.a, self.a.pm, 'transmittal'), [])

    def test_transmittal_on_an_archived_project_is_dropped(self):
        self.issue(self.a, users=[self.a.client])
        with org_context(self.a.org):
            Project.objects.filter(pk=self.a.job.pk).update(archived_at=timezone.now())
        self.assertEqual(self.items_of(self.a, self.a.client, 'transmittal'), [])

    def test_digest_includes_both(self):
        self.issue(self.a, users=[self.a.client], due=timezone.localdate() + timedelta(days=1))
        call_command('send_reminders', stdout=io.StringIO())
        by = {m.to[0]: m.body for m in mail.outbox}
        self.assertIn('TR-001 Joinery drawings for construction', by['client@alpha.test'])
        self.assertIn('Thin quote cover on Alpha bid', by['est@alpha.test'])
        self.assertNotIn('Thin quote cover', by.get('bid@alpha.test', ''))
        self.assertNotIn('Thin quote cover', by.get('client@alpha.test', ''))
