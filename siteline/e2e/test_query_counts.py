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

from tenancy import scale
from tenancy.context import close_request_memo, open_request_memo, org_context
from tenancy.models import Membership

SMALL, LARGE = 4, 12
VOLUMES = dict(rfis_per_job=4, diary_per_job=4, docs_per_job=4, apps_per_job=4, subapps_per_job=4, actuals_per_job=6)

# Queries for the Needs me count by the bell (every page) and for whole pages, at most
BADGE_CEILING = 30
PAGE_CEILING = 50


class QueryCountsDontGrowTests(TestCase):
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
            ('pm', u('diary_register', pk)), ('pm', u('document_list', pk)), ('pm', u('package_list', pk)),
            ('sub', u('project_detail', pk)), ('sub', u('subpay_list', pk)), ('sub', u('document_list', pk)),
            ('sub', u('needs_me')),
            ('client', u('project_detail', pk)), ('client', u('needs_me')), ('client', u('application_list', pk)),
        ]

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
