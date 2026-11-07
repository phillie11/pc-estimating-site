"""
Query counts must not grow with the size of the company.

Two companies are built with tenancy.scale (the data behind `manage.py seed_scale`): a small one
and one three times the size. For the pages that were worst in the performance crawl, and for the
Needs me badge that renders on every page, the same person's page must take no more queries in
the big company than in the small one. A page that loops over rows with a query each (an N+1)
fails here. The ceilings pin today's counts with a little headroom, so a new query on every page
is noticed too.
"""
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from security.testing import hold_rate_limit_clock
from tenancy import scale
from tenancy.context import close_request_memo, open_request_memo, org_context
from tenancy.models import Membership

SMALL, LARGE = 4, 12
VOLUMES = dict(rfis_per_job=4, diary_per_job=4, docs_per_job=4, apps_per_job=4, subapps_per_job=4, actuals_per_job=6,
               quality_per_job=(4, 4, 6))

# Queries for the Needs me count by the bell (every page) and for whole pages, at most
BADGE_CEILING = 30
PAGE_CEILING = 50


class QueryCountsDontGrowTests(TestCase):
    def setUp(self):
        # The search and export pages count each request against a rate limit, and the first request of a new
        # window takes extra queries (start the counter, clear finished ones). Held in one window, the small and
        # big company's pages are measured alike however slowly the suite runs.
        hold_rate_limit_clock(self)

    @classmethod
    def setUpTestData(cls):
        cls.small = scale.build('Small Fitout', jobs=SMALL, staff=6, subs=40, email_domain='small.test', **VOLUMES)
        cls.large = scale.build('Large Fitout', jobs=LARGE, staff=6, subs=120, email_domain='large.test', **VOLUMES)

    def _people(self, built):
        """owner, PM, subcontractor and client of the company's first live job."""
        from projects.models import ProjectMember
        with org_context(built.org):
            job = built.live[0]
            sub_m = ProjectMember.objects.filter(project=job, role=ProjectMember.ROLE_SUBCONTRACTOR).first()
            client_m = ProjectMember.objects.filter(project=job, role=ProjectMember.ROLE_CLIENT).first()
            return {'owner': built.owner, 'pm': job.manager, 'sub': sub_m.user, 'client': client_m.user}, job

    def _pages(self, built):
        people, job = self._people(built)
        s, pk = built.org.slug, job.pk
        u = lambda name, *a: reverse(name, args=[s, *a])  # noqa: E731
        return people, [
            ('owner', u('needs_me')), ('owner', u('rfi_register')), ('owner', u('project_list') + '?view=all'),
            ('owner', u('activity')), ('owner', u('pipeline')), ('owner', u('tender_coverage')),
            ('owner', u('delivery_home')), ('owner', u('estimating_home')), ('owner', u('subpay_cis')),
            ('pm', u('needs_me')), ('pm', u('project_detail', pk)), ('pm', u('contract_detail', pk)),
            ('pm', u('cost_report', pk)), ('pm', u('application_list', pk)), ('pm', u('subpay_list', pk)),
            ('pm', u('cvr_list', pk)), ('pm', u('cvr_cashflow', pk)), ('pm', u('daywork_list', pk)),
            ('owner', u('company_cvr')), ('pm', u('cvr_period', pk, self._signed_off_cvr(built, job))),
            ('pm', u('diary_register', pk)), ('pm', u('document_list', pk)), ('pm', u('package_list', pk)),
            ('pm', u('safety_home', pk)), ('pm', u('incident_list', pk)), ('pm', u('safety_on_site', pk)),
            ('pm', u('safety_sign_in_log', pk)), ('owner', u('company_incidents')),
            ('sub', u('project_detail', pk)), ('sub', u('subpay_list', pk)), ('sub', u('document_list', pk)),
            ('sub', u('needs_me')),
            ('client', u('project_detail', pk)), ('client', u('needs_me')), ('client', u('application_list', pk)),
            # Drawing viewer with mark-up layers, and revision compare (stream G)
            *self._drawing_pages(built, job, u),
            # Programme look-ahead and meetings (Release 3, stream C)
            ('pm', u('programme', pk)), ('pm', u('programme_lookahead', pk)), ('pm', u('meeting_list', pk)),
            ('pm', u('meeting_actions', pk) + '?show=all'), ('pm', u('meeting_detail', pk, self._meeting(built, job))),
            ('sub', u('programme_lookahead', pk)), ('sub', u('meeting_list', pk)),
            ('client', u('programme_lookahead', pk)), ('client', u('meeting_list', pk)),
            # Close-out (closeout/)
            ('pm', u('closeout_overview', pk)), ('pm', u('closeout_defects', pk)), ('pm', u('closeout_om', pk)),
            ('pm', u('closeout_packs', pk)), ('pm', u('closeout_certificates', pk)), ('client', u('closeout_client', pk)),
            # Quality (quality/): the three registers and the busiest record pages
            ('pm', u('inspection_list', pk) + '?status=all'), ('pm', u('permit_list', pk) + '?status=all'),
            ('pm', u('penetration_list', pk)), *self._quality_pages(built, job, u),
            # Company reports and Settings > API (reports/, api/). The cash position reads every live job's cashflow
            # at once (cvr.cashflow.build_many)
            ('owner', u('reports_home')), ('owner', u('report_margin')), ('owner', u('report_cash')),
            ('owner', u('report_overdue')),
            ('owner', u('report_safety')), ('owner', u('report_subcontractors')), ('owner', u('api_settings')),
            ('owner', u('api_docs')),
            # The sub's and client's portal pages for the wave 2 defaults (quality/portal.py, safety/portal.py,
            # cvr/portal.py), and the busiest staff pages they changed
            ('sub', u('portal_permits', pk)), ('sub', u('portal_firestop', pk)), ('sub', u('portal_near_miss', pk)),
            ('sub', u('project_decisions', pk)), ('client', u('daywork_portal', pk)),
            ('client', u('project_decisions', pk)),
            # Global search (help/search.py): common letters, so every source finds something
            ('owner', u('search') + '?q=an'), ('pm', u('search') + '?q=an'), ('sub', u('search') + '?q=an'),
            ('client', u('search') + '?q=an'),
        ]

    def _meeting(self, built, job):
        """The job's latest issued meeting (the one with the most brought forward)."""
        from meetings.models import Meeting
        with org_context(built.org):
            return Meeting.objects.filter(project=job, status=Meeting.STATUS_ISSUED).order_by('-held_on').first().pk

    def _signed_off_cvr(self, built, job):
        """The job's oldest CVR, signed off: its page also checks the month's figures for later changes."""
        from cvr.models import CVRPeriod
        with org_context(built.org):
            return CVRPeriod.objects.filter(project=job, status=CVRPeriod.SIGNED_OFF).order_by('month').first().pk

    def _drawing_pages(self, built, job, u):
        """A shared drawing with two revisions, marked up by one person per job in the company (so more in the big one)."""
        from projects.models import Document
        from tenancy.models import Membership as M
        from viewer.models import MarkupLayer
        with org_context(built.org):
            doc = next(d for d in Document.objects.filter(project=job, visibility=Document.VIS_SHARED).order_by('pk')
                       if d.revisions.count() == 2)
            cur = doc.revisions.get(is_current=True)
            staff = [m.user for m in M.objects.filter(organization=built.org).exclude(role=M.ROLE_EXTERNAL)
                     .select_related('user').order_by('pk')][:len(built.live)]
            for i, user in enumerate(staff):
                MarkupLayer.objects.get_or_create(
                    revision=cur, owner=user, defaults={'project': job, 'document': doc,
                                                        'share': MarkupLayer.EXTERNAL if i % 2 else MarkupLayer.TEAM,
                                                        'data': {'v': 1, 'shapes': []}})
        return [('pm', u('drawing_viewer', job.pk, doc.pk, cur.pk)), ('pm', u('drawing_compare', job.pk, doc.pk)),
                ('sub', u('drawing_viewer', job.pk, doc.pk, cur.pk)), ('client', u('drawing_compare', job.pk, doc.pk))]

    def _quality_pages(self, built, job, u):
        from quality.models import Inspection, Penetration, Permit
        with org_context(built.org):
            ins = Inspection.objects.filter(project=job, status=Inspection.IN_PROGRESS).order_by('pk').first()
            ptw = Permit.objects.filter(project=job).order_by('pk').first()
            pen = Penetration.objects.filter(project=job).order_by('pk').first()
        return [('pm', u('inspection_detail', job.pk, ins.pk)), ('pm', u('permit_detail', job.pk, ptw.pk)),
                ('pm', u('penetration_detail', job.pk, pen.pk))]

    def _count(self, user, url):
        c = Client()
        c.force_login(user, backend='django.contrib.auth.backends.ModelBackend')
        c.get(url)  # first visits write (project visits, last workspace); measure the second
        with CaptureQueriesContext(connection) as ctx:
            r = c.get(url)
        self.assertEqual(r.status_code, 200, url)
        return len(ctx.captured_queries)

    def test_pages_take_the_same_queries_in_a_company_three_times_the_size(self):
        small_people, small_pages = self._pages(self.small)
        large_people, large_pages = self._pages(self.large)
        for (role, small_url), (_, large_url) in zip(small_pages, large_pages):
            with self.subTest(role=role, page=large_url.split('/', 2)[-1]):
                small_n = self._count(small_people[role], small_url)
                large_n = self._count(large_people[role], large_url)
                self.assertLessEqual(large_n, small_n, f'{role} {large_url}: {small_n} queries grew to {large_n}')
                self.assertLessEqual(large_n, PAGE_CEILING, f'{role} {large_url}: {large_n} queries')

    def _api_count(self, built, path):
        from api import auth
        from api.models import SCOPE_CHOICES
        with org_context(built.org):
            _, text = auth.create_token(built.org, built.owner, 'Counts', [c for c, _ in SCOPE_CHOICES])
        c = Client()
        c.get(path, HTTP_AUTHORIZATION=f'Bearer {text}')
        with CaptureQueriesContext(connection) as ctx:
            r = c.get(path, HTTP_AUTHORIZATION=f'Bearer {text}')
        self.assertEqual(r.status_code, 200, path)
        return len(ctx.captured_queries)

    def test_api_lists_take_the_same_queries_in_a_company_three_times_the_size(self):
        """Every API list endpoint (api/resources.py), a full page each, as the company grows."""
        from api.resources import resources
        for r in resources():
            path = reverse(r.list_url_name()) + '?page_size=200'
            with self.subTest(r.name):
                small_n, large_n = self._api_count(self.small, path), self._api_count(self.large, path)
                self.assertLessEqual(large_n, small_n, f'{path}: {small_n} queries grew to {large_n}')
                self.assertLessEqual(large_n, PAGE_CEILING, f'{path}: {large_n} queries')

    def _badge(self, built, user):
        from actions import registry
        m = Membership.objects.get(organization=built.org, user=user)
        memo = open_request_memo()
        try:
            with org_context(built.org):
                req = registry.request_for(user, m)
                with CaptureQueriesContext(connection) as ctx:
                    registry.count_for_user(req)
        finally:
            close_request_memo(memo)
        return len(ctx.captured_queries)

    def test_needs_me_badge_is_fixed_queries(self):
        small_people, _ = self._people(self.small)
        large_people, _ = self._people(self.large)
        for role in ('owner', 'pm', 'sub', 'client'):
            with self.subTest(role=role):
                small_n, large_n = self._badge(self.small, small_people[role]), self._badge(self.large, large_people[role])
                self.assertLessEqual(large_n, small_n, f'{role}: badge {small_n} queries grew to {large_n}')
                self.assertLessEqual(large_n, BADGE_CEILING, f'{role}: badge {large_n} queries')

    def test_organisation_row_is_not_fetched_per_record(self):
        """
        Every get_absolute_url() reads organization.slug. Inside the request's organisation that is
        the organisation already loaded, not a query per row (tenancy.models._CurrentOrgDescriptor).
        """
        from rfis.models import RFI
        with org_context(self.large.org):
            rfis = list(RFI.objects.all()[:20])
            with CaptureQueriesContext(connection) as ctx:
                [r.get_absolute_url() for r in rfis]
        self.assertEqual(len([q for q in ctx.captured_queries if 'tenancy_organization' in q['sql']]), 0)
