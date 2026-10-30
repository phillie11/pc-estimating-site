"""
Trade coverage and the chase board: counts per trade, the bulk chase (one reminder per
subcontractor still to quote, at most once a day), suggestions, the company-wide view,
the alerts hook, and that a tendering subcontractor never sees any of it.
"""
import io
from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from directory.models import Package, Subcontractor, Trade
from jobs.models import Job
from projects.models import Project, ProjectInvite, ProjectMember
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership, Notification
from tenancy.tests import Base, make_user

from . import actions, coverage
from .models import TenderSettings


class CoverageBase(Base):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.now = timezone.now()
        self.dan = make_user('dan@dry.test', 'Dan Dry')       # quoted
        self.eve = make_user('eve@part.test', 'Eve Part')     # outstanding, portal user
        self.kit = make_user('kit@join.test', 'Kit Join')     # outstanding on two trades
        self.est = make_user('est@a.test', 'Eve Estimator')
        self.pm = make_user('pm@a.test', 'Pat PM')
        Membership.objects.create(organization=self.org_a, user=self.est, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_ESTIMATING)
        Membership.objects.create(organization=self.org_a, user=self.pm, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        with org_context(self.org_a):
            self.bid = Project.objects.create(name='Office fit-out', stage=Project.STAGE_TENDERING, owner=self.alice,
                                              site_address='Kingsway, London WC2', value=Decimal('900000'),
                                              quotes_due=self.today + timedelta(days=2))
            self.dry = Trade.objects.get_or_create(name='Drylining')[0]
            self.join = Trade.objects.get_or_create(name='Joinery')[0]
            self.elec = Trade.objects.get_or_create(name='Electrical')[0]
            mk = Subcontractor.objects.create
            self.s_dan = mk(company_name='Dan Dry Ltd', email='dan@dry.test', status='APPROVED')
            self.s_eve = mk(company_name='Eve Partitions', email='eve@part.test', status='APPROVED')
            self.s_new = mk(company_name='New Walls', email='new@walls.test', status='APPROVED')
            self.s_dec = mk(company_name='Declined Dry', email='no@dry.test', status='APPROVED')
            self.s_kit = mk(company_name='Kit Joinery', email='kit@join.test', status='APPROVED')
            self.s_spark = mk(company_name='Sparks', email='s@sparks.test', status='APPROVED')
            for u, s in ((self.dan, self.s_dan), (self.eve, self.s_eve), (self.kit, self.s_kit)):
                Membership.objects.create(organization=self.org_a, user=u, role=Membership.ROLE_EXTERNAL)
                ProjectMember.objects.create(project=self.bid, user=u, role=ProjectMember.ROLE_BIDDER,
                                             subcontractor=s, company_name=s.company_name)
            ProjectInvite.objects.create(project=self.bid, email='new@walls.test', role=ProjectMember.ROLE_BIDDER,
                                         subcontractor=self.s_new, company_name='New Walls')
            due = self.today + timedelta(days=2)

            def pkg(sub, trade, status='ENQUIRY', value=None, viewed=False):
                return Package.objects.create(
                    project=self.bid, subcontractor=sub, trade=trade, status=status, quote_due=due, scope='Walls',
                    viewed_at=self.now if viewed else None, sent_at=self.now,
                    quote_value=Decimal(value) if value else None, submitted_at=self.now if value else None)
            self.p_dan = pkg(self.s_dan, self.dry, 'QUOTED', '10000', viewed=True)
            self.p_eve = pkg(self.s_eve, self.dry, viewed=True)
            self.p_new = pkg(self.s_new, self.dry)
            self.p_dec = pkg(self.s_dec, self.dry, 'DECLINED', viewed=True)
            self.p_kit1 = pkg(self.s_kit, self.join)
            self.p_kit2 = pkg(self.s_kit, self.elec)
            for i in range(3):  # electrical is covered by three quotes
                s = mk(company_name=f'Elec {i}', email=f'e{i}@e.test', status='APPROVED')
                pkg(s, self.elec, 'QUOTED', '5000')

    def rows(self, target=None):
        with org_context(self.org_a):
            return {r.trade_name: r for r in coverage.trade_rows(Project.objects.get(pk=self.bid.pk), target=target)}

    def tender(self):
        return self.url('tender', self.org_a, self.bid.pk)


class CountTests(CoverageBase):
    def test_counts_per_trade(self):
        r = self.rows()
        got = {name: (x.invited, x.viewed, x.quoted, x.declined, x.outstanding, x.thin) for name, x in r.items()}
        self.assertEqual(got, {
            'Drylining': (4, 3, 1, 1, 2, True),
            'Joinery': (1, 0, 0, 0, 1, True),
            'Electrical': (4, 0, 3, 0, 1, False),
        })
        self.assertEqual((r['Drylining'].days_to_due, r['Drylining'].due_label), (2, '2 days to quotes due'))
        self.assertEqual(r['Drylining'].status_label, 'Thin: 1 of 3')

    def test_target_is_per_organisation(self):
        with org_context(self.org_a):
            TenderSettings.objects.create(min_quotes=1)
            self.assertEqual(TenderSettings.for_org(self.org_a).min_quotes, 1)
        self.assertFalse(self.rows()['Drylining'].thin)
        self.assertTrue(self.rows()['Joinery'].thin)
        with org_context(self.org_b):
            self.assertEqual(TenderSettings.for_org(self.org_b).min_quotes, 3)

    def test_appointed_trade_is_not_thin(self):
        with org_context(self.org_a):
            Package.objects.filter(pk=self.p_kit1.pk).update(status=Package.APPOINTED)
        self.assertEqual(self.rows()['Joinery'].status_label, 'Appointed')

    def test_due_labels(self):
        self.assertEqual([coverage.due_label(d) for d in (None, -1, -3, 0, 1, 5)],
                         ['No quote date', '1 day overdue', '3 days overdue', 'Quotes due today',
                          '1 day to quotes due', '5 days to quotes due'])

    def test_tender_page_shows_board_with_red_rows(self):
        self.login(self.alice)
        r = self.client.get(self.tender())
        self.assertContains(r, 'Trade coverage')
        self.assertContains(r, 'class="thin"', count=2)
        self.assertContains(r, 'Chase 3 still to quote')


class ChaseTests(CoverageBase):
    def chase(self):
        self.login(self.alice)
        return self.client.post(self.tender(), {'action': 'chase'})

    def work(self):
        call_command('run_worker', '--burst', stdout=io.StringIO())

    def test_one_reminder_per_sub_still_to_quote(self):
        mail.outbox.clear()
        r = self.chase()
        self.assertRedirects(r, self.tender() + '#coverage', fetch_redirect_response=False)
        self.assertEqual(Job.objects.filter(name='tenders.chase_outstanding').count(), 1)
        self.work()
        notes = Notification.all_objects.filter(title__startswith='Reminder')
        self.assertEqual(sorted(n.user.email for n in notes), ['eve@part.test', 'kit@join.test'])
        kit = notes.get(user=self.kit)
        self.assertIn('Electrical, Joinery', kit.title)  # one notification covering both trades
        self.assertEqual(kit.url, self.tender())
        # Not signed up yet: the portal invitation link again, by email
        invite = ProjectInvite.all_objects.get(email='new@walls.test')
        new_mail = [m for m in mail.outbox if 'new@walls.test' in m.to]
        self.assertEqual(len(new_mail), 1)
        self.assertIn(f'/invite/project/{invite.token}/', new_mail[0].body)
        # Quoted and declined subs are left alone
        self.assertFalse(Notification.all_objects.filter(user=self.dan).exists())
        self.assertFalse(any('no@dry.test' in m.to for m in mail.outbox))
        # The chase is logged on each package
        chased = Package.all_objects.filter(chased_at__isnull=False)
        self.assertEqual(set(chased.values_list('pk', flat=True)),
                         {self.p_eve.pk, self.p_new.pk, self.p_kit1.pk, self.p_kit2.pk})
        self.assertEqual(set(chased.values_list('chase_count', flat=True)), {1})

    def test_idempotent_per_day(self):
        self.chase()
        self.work()
        before = Notification.all_objects.count(), len(mail.outbox)
        r = self.client.post(self.tender(), {'action': 'chase'}, follow=True)
        self.assertContains(r, 'already been chased today')
        self.assertEqual(Job.objects.filter(name='tenders.chase_outstanding').count(), 1)
        # Even if the job itself runs again (at-least-once delivery), nobody hears twice
        job = Job.objects.get()
        with org_context(self.org_a):
            told = coverage.run_chase(Project.objects.get(pk=self.bid.pk), job.payload['package_ids'])
        self.assertEqual(told, 0)
        self.assertEqual((Notification.all_objects.count(), len(mail.outbox)), before)

    def test_double_click_is_one_job_and_tomorrow_chases_again(self):
        self.chase()
        self.chase()
        self.assertEqual(Job.objects.filter(name='tenders.chase_outstanding').count(), 1)
        self.work()
        with org_context(self.org_a):
            Package.objects.filter(chased_at__isnull=False).update(chased_at=self.now - timedelta(days=1))
        self.chase()
        self.work()
        self.assertEqual(Notification.all_objects.filter(user=self.eve, title__startswith='Reminder').count(), 2)
        self.assertEqual(Package.all_objects.get(pk=self.p_eve.pk).chase_count, 2)

    def test_new_invitee_after_a_chase_is_chased_once(self):
        self.chase()
        self.work()
        with org_context(self.org_a):
            late = Subcontractor.objects.create(company_name='Late Dry', email='eve@part.test')
            Package.objects.create(project=self.bid, subcontractor=late, trade=self.dry, status='ENQUIRY')
            ProjectMember.objects.filter(user=self.eve).update(subcontractor=late)
        self.chase()
        self.work()
        self.assertEqual(Job.objects.filter(name='tenders.chase_outstanding').count(), 2)

    def test_expired_invite_is_refreshed_not_sent_dead(self):
        """Review defect 7: the chase re-sent an invitation link that had already expired."""
        invite = ProjectInvite.all_objects.get(email='new@walls.test')
        ProjectInvite.all_objects.filter(pk=invite.pk).update(expires_at=self.now - timedelta(days=1))
        mail.outbox.clear()
        self.chase()
        self.work()
        invite.refresh_from_db()
        self.assertTrue(invite.is_valid())
        self.assertGreater(invite.expires_at, self.now + timedelta(days=20))
        self.assertEqual(len([m for m in mail.outbox if 'new@walls.test' in m.to]), 1)
        # The link in the email now opens the invitation rather than "expired"
        self.client.logout()
        r = self.client.get(f'/invite/project/{invite.token}/')
        self.assertNotContains(r, 'expired')

    def test_failure_half_way_leaves_packages_unchased(self):
        """Review defect 8: the task is atomic, so a crash after claiming doesn't mark anyone chased unnotified."""
        from unittest import mock

        from jobs import get_task
        self.assertTrue(get_task('tenders.chase_outstanding').atomic)
        self.chase()
        with mock.patch('tenancy.notify.alert', side_effect=RuntimeError('mail server down')):
            self.work()
        self.assertFalse(Package.all_objects.filter(chased_at__isnull=False).exists())
        self.assertEqual(Job.objects.get().status, Job.FAILED)  # will be retried

    def test_only_staff_can_chase(self):
        self.login(self.eve)
        self.client.post(self.tender(), {'action': 'chase'})
        self.assertFalse(Job.objects.filter(name='tenders.chase_outstanding').exists())


class SuggestionTests(CoverageBase):
    def setUp(self):
        super().setUp()
        soon, later = self.today + timedelta(days=10), self.today + timedelta(days=300)
        with org_context(self.org_a):
            mk = Subcontractor.objects.create

            def sub(name, insured_until, regions, **kw):
                s = mk(company_name=name, email=f'{name.split()[0].lower()}@x.test', status=kw.pop('status', 'APPROVED'),
                       public_liability_expiry=insured_until, employers_liability_expiry=insured_until,
                       region_codes=regions, **kw)
                s.trades.add(self.dry)
                return s
            self.expired = sub('Expired Walls', self.today - timedelta(days=1), ['LDN'])
            self.london = sub('London Walls', later, ['LDN'], rating=3)
            self.national = sub('National Walls', later, ['UK'], rating=5)
            self.north = sub('Northern Walls', later, ['NW'], rating=5)
            self.expiring = sub('Expiring Walls', soon, ['LDN'])
            self.unknown = sub('Unknown Walls', None, [])
            sub('Pending Walls', later, ['LDN'], status='PENDING')
            sub('Archived Walls', later, ['LDN'], archived_at=self.now)
            sub('Noemail Walls', later, ['LDN'])
            Subcontractor.objects.filter(company_name='Noemail Walls').update(email='')
            self.s_eve.trades.add(self.dry)  # already invited: never suggested

    def test_order_and_filters(self):
        with org_context(self.org_a):
            got = coverage.suggest(Project.objects.get(pk=self.bid.pk), self.dry, limit=20)
        self.assertEqual([s.company_name for s in got],
                         # insured first; then covers the site (London, or nationwide); then rating
                         ['National Walls', 'London Walls', 'Northern Walls', 'Expiring Walls', 'Unknown Walls',
                          'Expired Walls'])
        self.assertEqual({s.company_name: s.region_fit for s in got}['Northern Walls'], 'no')

    def test_shown_on_board_and_invite_uses_same_scope(self):
        self.login(self.alice)
        r = self.client.get(self.tender())
        self.assertContains(r, 'Suggest more for Drylining')
        self.assertContains(r, 'Region matching is approximate')
        self.assertContains(r, 'London Walls')
        r = self.client.post(self.tender(), {'action': 'invite_suggested', 'trade': self.dry.pk,
                                             'subcontractor': self.london.pk})
        pkg = Package.all_objects.get(subcontractor=self.london)
        self.assertEqual((pkg.trade, pkg.scope, pkg.quote_due), (self.dry, 'Walls', self.today + timedelta(days=2)))
        # Twice is still one invitation
        self.client.post(self.tender(), {'action': 'invite_suggested', 'trade': self.dry.pk,
                                         'subcontractor': self.london.pk})
        self.assertEqual(Package.all_objects.filter(subcontractor=self.london).count(), 1)

    def test_cannot_invite_another_companys_sub(self):
        with org_context(self.org_b):
            theirs = Subcontractor.objects.create(company_name='Bravo Sub', email='b@b.test')
        self.login(self.alice)
        r = self.client.post(self.tender(), {'action': 'invite_suggested', 'trade': self.dry.pk,
                                             'subcontractor': theirs.pk})
        self.assertEqual(r.status_code, 404)


class CompanyWideTests(CoverageBase):
    def test_board_and_filters(self):
        with org_context(self.org_a):
            Project.objects.create(name='Submitted bid', stage=Project.STAGE_SUBMITTED)
        self.login(self.est)
        r = self.client.get(self.url('tender_coverage', self.org_a))
        self.assertContains(r, 'Office fit-out')
        self.assertContains(r, '2 thin trades')
        r = self.client.get(self.url('tender_coverage', self.org_a) + '?show=thin')
        self.assertNotContains(r, '<strong>Electrical</strong>')
        self.assertContains(r, '<strong>Joinery</strong>')
        r = self.client.get(self.url('tender_coverage', self.org_a) + f'?lead={self.est.pk}')
        self.assertContains(r, 'This bid lead has no trades out')

    def test_empty_state(self):
        self.login(self.bob)
        r = self.client.get(self.url('tender_coverage', self.org_b))
        self.assertContains(r, 'No open tenders out to subcontractors')

    def test_roles(self):
        for user, code in ((self.alice, 200), (self.est, 200), (self.pm, 403), (self.eve, 403), (self.bob, 404)):
            self.login(user)
            with self.subTest(user=user.email):
                self.assertEqual(self.client.get(self.url('tender_coverage', self.org_a)).status_code, code)


class AlertTests(CoverageBase):
    class Req:
        def __init__(self, user, membership):
            self.user, self.membership = user, membership

    def req(self, user):
        return self.Req(user, Membership.objects.get(organization=self.org_a, user=user))

    def test_due_in_three_days_with_thin_cover(self):
        with org_context(self.org_a):
            got = actions.coverage_alerts(self.req(self.alice))
            self.assertEqual(len(got), 1)
            a = got[0]
            self.assertEqual((a['kind'], a['project'].pk, a['tone']), ('tenders.thin_coverage', self.bid.pk, 'warn'))
            self.assertEqual(a['trades'], ['Joinery', 'Drylining'])  # furthest short first
            self.assertIn('Drylining (1 of 3)', a['detail'])
            self.assertTrue(a['url'].endswith('/tender/#coverage'))
            self.assertEqual(actions.coverage_alerts_for_org(self.org_a), got)
            # Every estimator gets every alert, company-wide; people outside estimating get none
            self.assertEqual(actions.coverage_alerts_for_org(self.org_a, user=self.est), got)
            self.assertEqual(actions.coverage_alerts_for_org(self.org_a, user=self.pm), [])
            self.assertEqual(actions.coverage_alerts_for_org(self.org_a, user=self.eve), [])
            self.assertEqual({u.email for u in actions.estimating_recipients(self.org_a)}, {'alice@a.test', 'est@a.test'})
            # Further off, or covered: nothing
            Project.objects.filter(pk=self.bid.pk).update(quotes_due=self.today + timedelta(days=4))
            self.assertEqual(actions.coverage_alerts_for_org(self.org_a), [])
            Project.objects.filter(pk=self.bid.pk).update(quotes_due=self.today)
            TenderSettings.objects.create(min_quotes=1)
            self.assertEqual(actions.coverage_alerts_for_org(self.org_a)[0]['trades'], ['Joinery'])

    def test_externals_and_delivery_only_get_nothing(self):
        with org_context(self.org_a):
            self.assertEqual(actions.coverage_alerts(self.req(self.eve)), [])
            self.assertEqual(actions.coverage_alerts(self.req(self.pm)), [])

    def test_no_org_context_fails_closed_and_orgs_stay_apart(self):
        with self.assertRaises(TenantContextMissing):
            actions.coverage_alerts_for_org(self.org_a)
        with org_context(self.org_b):
            self.assertEqual(actions.coverage_alerts_for_org(self.org_b), [])


class BidderIsolationTests(CoverageBase):
    """A tendering subcontractor never sees other bidders, counts or coverage."""

    def test_bidder_tender_page_has_no_coverage_or_other_bidders(self):
        self.login(self.eve)
        r = self.client.get(self.tender())
        self.assertEqual(r.status_code, 200)
        for text in ('Trade coverage', 'Dan Dry Ltd', 'New Walls', 'Declined Dry', 'Kit Joinery', 'Thin', 'Chase',
                     'Suggest more', 'Bid or no bid', ' of 3'):
            self.assertNotContains(r, text)
        self.assertNotIn('rows', r.context)

    def test_bidder_cannot_reach_coverage_or_bid_pages(self):
        self.login(self.eve)
        for url in (self.url('tender_coverage', self.org_a), self.url('bid_gate', self.org_a, self.bid.pk),
                    self.url('bid_lost', self.org_a, self.bid.pk), self.url('tender_settings', self.org_a)):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_bidder_post_of_staff_actions_does_nothing(self):
        self.login(self.eve)
        self.client.post(self.tender(), {'action': 'invite_suggested', 'trade': self.dry.pk,
                                         'subcontractor': self.s_spark.pk})
        self.assertFalse(Package.all_objects.filter(subcontractor=self.s_spark).exists())

    def test_other_company_cannot_see_the_board(self):
        self.login(self.bob)
        self.assertEqual(self.client.get(self.url('tender', self.org_b, self.bid.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.url('tender', self.org_b, self.bid.pk),
                                          {'action': 'chase'}).status_code, 404)
        self.assertFalse(Job.objects.filter(name='tenders.chase_outstanding').exists())
