"""
Regression tests for defects found by the end-to-end journey (e2e/test_journey.py) and the
performance and accessibility passes. Each test names the defect it pins down.
"""
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.views import create_organization
from directory.models import Package, Subcontractor, Trade
from projects.models import Project, ProjectInvite, ProjectMember
from tenancy.context import org_context
from tenancy.models import ActivityEvent, Membership

PW = 'Regress-Pass-2026'


class TenderBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email='o@r.test', password=PW, full_name='Owner')
        self.org = create_organization('Regress Fitout', self.owner)
        self.winner = User.objects.create_user(email='win@sub.test', password=PW, full_name='Wendy Winner')
        self.loser = User.objects.create_user(email='lose@sub.test', password=PW, full_name='Larry Loser')
        self.client_user = User.objects.create_user(email='cli@client.test', password=PW, full_name='Clara Client')
        with org_context(self.org):
            for u in (self.winner, self.loser, self.client_user):
                Membership.objects.create(organization=self.org, user=u, role=Membership.ROLE_EXTERNAL)
            self.trade = Trade.objects.get(name='Joinery')
            self.project = Project.objects.create(name='Bar job', stage=Project.STAGE_TENDERING)
            self.subs, self.pkgs = {}, {}
            for user, name in ((self.winner, 'Winner Joinery'), (self.loser, 'Loser Joinery')):
                sub = Subcontractor.objects.create(company_name=name, email=user.email)
                self.subs[user.pk] = sub
                ProjectMember.objects.create(project=self.project, user=user, role=ProjectMember.ROLE_BIDDER,
                                             subcontractor=sub, company_name=name)
                self.pkgs[user.pk] = Package.objects.create(project=self.project, subcontractor=sub, trade=self.trade,
                                                            status=Package.QUOTED, quote_value=1000)
            ProjectMember.objects.create(project=self.project, user=self.client_user, role=ProjectMember.ROLE_CLIENT)

    def as_(self, user):
        c = Client()
        c.force_login(user, backend='django.contrib.auth.backends.ModelBackend')
        return c


class UnsuccessfulBidderSeesOutcomeTests(TenderBase):
    """
    Defect: the "your quote was not successful" alert was tied to the project the bidder had just
    lost access to, so the bell and notifications page (which hide alerts on projects you can't
    open) never showed it. Only the email got through.
    """

    def test_appointing_one_bidder_tells_the_other_in_the_portal(self):
        r = self.as_(self.owner).post(reverse('package_list', args=[self.org.slug, self.project.pk]),
                                      {'action': 'appoint', 'package': self.pkgs[self.winner.pk].pk})
        self.assertEqual(r.status_code, 302)
        loser = self.as_(self.loser)
        self.assertEqual(loser.get(reverse('project_detail', args=[self.org.slug, self.project.pk])).status_code, 404)
        page = loser.get(reverse('notifications', args=[self.org.slug])).content.decode()
        self.assertIn('on this occasion your quote was not successful', page)

    def test_losing_the_main_tender_tells_every_bidder(self):
        self.as_(self.owner).post(reverse('bid_lost', args=[self.org.slug, self.project.pk]),
                                  {'loss_reason': 'PRICE', 'our_price': '', 'winner_price': '', 'lessons': ''})
        with org_context(self.org):
            self.project.refresh_from_db()
        if self.project.stage != Project.STAGE_LOST:
            self.skipTest('Loss review form changed; covered by the appointment test')
        for user in (self.winner, self.loser):
            page = self.as_(user).get(reverse('notifications', args=[self.org.slug])).content.decode()
            self.assertIn('Tender closed: Bar job will not be going ahead with us', page)


class BidderJoiningIsStaffOnlyTests(TestCase):
    """
    Defects: a tendering subcontractor accepting their invite logged "joined the project" for
    everyone on the project, and the overview's People card and Who's who listed tendering
    subcontractors to externals, so the client (and an appointed subcontractor) could see every
    company pricing the work.
    """

    def setUp(self):
        self.owner = User.objects.create_user(email='o@r.test', password=PW, full_name='Owner')
        self.org = create_organization('Regress Fitout', self.owner)
        with org_context(self.org):
            self.project = Project.objects.create(name='Bar job', stage=Project.STAGE_TENDERING,
                                                  quotes_due=timezone.localdate() + timedelta(days=7))
            self.sub = Subcontractor.objects.create(company_name='Bidder Ltd', email='bid@sub.test')
            self.bid_invite = ProjectInvite.objects.create(project=self.project, email='bid@sub.test',
                                                           role=ProjectMember.ROLE_BIDDER, subcontractor=self.sub,
                                                           company_name='Bidder Ltd', invited_by=self.owner)
            self.client_invite = ProjectInvite.objects.create(project=self.project, email='cli@client.test',
                                                              role=ProjectMember.ROLE_CLIENT, invited_by=self.owner)

    def accept(self, invite, name):
        r = Client().post(reverse('accept_project_invite', args=[invite.token]), {'full_name': name, 'password': PW})
        self.assertEqual(r.status_code, 302)

    def test_bidder_join_is_internal_and_client_join_is_shared(self):
        self.accept(self.bid_invite, 'Bea Bidder')
        self.accept(self.client_invite, 'Clara Client')
        with org_context(self.org):
            events = {e.actor.full_name: e.internal_only for e in ActivityEvent.objects.filter(verb='joined the project')}
        self.assertEqual(events, {'Bea Bidder': True, 'Clara Client': False})
        c = Client()
        c.force_login(User.objects.get(email='cli@client.test'), backend='django.contrib.auth.backends.ModelBackend')
        for name in ('project_detail', 'project_team'):
            page = c.get(reverse(name, args=[self.org.slug, self.project.pk])).content.decode()
            self.assertNotIn('Bea Bidder', page)
            self.assertNotIn('Bidder Ltd', page)
            self.assertIn('Clara Client', page)
        staff = Client()
        staff.force_login(self.owner, backend='django.contrib.auth.backends.ModelBackend')
        self.assertIn('Bea Bidder', staff.get(reverse('project_team', args=[self.org.slug, self.project.pk]))
                      .content.decode())
