"""
Every Needs me provider, for every demo login, on whichever database the suite runs on.

Wave 1 shipped a provider whose UNION worked on SQLite and MySQL and failed on PostgreSQL
(untyped NULLs in the first branch). A provider's query only fails when it runs, and most
module tests run their own provider against their own small set-up, so a database-specific
mistake in one provider can hide until production. CI runs the whole suite on SQLite,
PostgreSQL and MySQL, so this file runs each registered provider on its own, for each person
in the demo company (seed_demo: staff in each section and every kind of external), and fails
on the database where any of them breaks, naming the provider and the person.

It also checks the shape of what comes back (a link, a title, a date rather than a time) and,
through the Needs me page itself, that the badge and the page agree for each login. Providers
that list nothing for anyone in the demo would only prove their query compiles, so the demo
must give each one something to list, except the few named in NOT_IN_DEMO with the reason.
"""
import datetime
import io
import shutil
import tempfile

from django.core.management import call_command
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from actions import registry
from tenancy.context import close_request_memo, open_request_memo, org_context
from tenancy.models import Membership, Organization

DEMO_ORG = 'northbank-interiors'
DEMO_LOGINS = ('owner@demo.test', 'estimator@demo.test', 'pm@demo.test', 'client@demo.test', 'sub@demo.test',
               'bids@demo.test', 'designer@demo.test')

# Providers the demo data gives nobody anything to do for, with why. Their queries still run above for every
# login (an empty result is still a query on this database); this only excuses them from listing something.
NOT_IN_DEMO = {}


class EveryProviderForEveryDemoLoginTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._media = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls._media, ignore_errors=True)
        cls.enterClassContext(override_settings(MEDIA_ROOT=cls._media))
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', stdout=io.StringIO())
        cls.org = Organization.objects.get(slug=DEMO_ORG)

    def _people(self):
        """Every active member of the demo company: the demo logins plus anyone else seed_demo adds."""
        rows = Membership.objects.filter(organization=self.org, is_active=True).select_related('user') \
            .order_by('user__email')
        people = [(m.user, m) for m in rows]
        self.assertTrue(set(DEMO_LOGINS) <= {u.email for u, _ in people}, 'a demo login is missing')
        return people

    def _run(self, provider, user, membership):
        memo = open_request_memo()
        try:
            with org_context(self.org):
                request = registry.request_for(user, membership)
                # A savepoint each, so one provider failing on PostgreSQL doesn't abort the rest of the transaction
                with transaction.atomic():
                    return list(provider.items(request))
        finally:
            close_request_memo(memo)

    def assertItemShape(self, item, provider, email):
        where = f'{provider.kind} for {email}'
        self.assertIsInstance(item, registry.Item, where)
        self.assertTrue(item.title and isinstance(item.title, str), f'{where}: no title')
        self.assertTrue(item.url.startswith('/'), f'{where}: link {item.url!r} is not a path on this site')
        # A datetime is a date too, but sorting and the Overdue / Today groups compare dates
        self.assertTrue(item.due is None or type(item.due) is datetime.date, f'{where}: due {item.due!r}')
        if item.project is not None:
            self.assertEqual(item.project.organization_id, self.org.pk, f'{where}: another company\'s job')

    def test_every_provider_runs_for_every_demo_login(self):
        listed = set()
        for user, membership in self._people():
            for provider in registry.providers():
                with self.subTest(provider=provider.kind, login=user.email):
                    items = self._run(provider, user, membership)
                    for item in items:
                        self.assertItemShape(item, provider, user.email)
                    if items:
                        listed.add(provider.kind)
        everyone = {p.kind for p in registry.providers()}
        silent = everyone - listed - set(NOT_IN_DEMO)
        self.assertFalse(silent, f'providers with nothing to list for anyone in the demo: {sorted(silent)}. '
                                 'Give them demo data (their own demo.py) so their query runs with rows, or add '
                                 'them to NOT_IN_DEMO with the reason.')
        stale = set(NOT_IN_DEMO) - everyone
        self.assertFalse(stale, f'NOT_IN_DEMO names providers that are not registered: {sorted(stale)}')

    def test_needs_me_page_and_badge_agree_for_every_demo_login(self):
        url = reverse('needs_me', args=[DEMO_ORG])
        for email in DEMO_LOGINS:
            with self.subTest(login=email):
                c = Client()
                c.force_login(User.objects.get(email=email), backend='django.contrib.auth.backends.ModelBackend')
                r = c.get(url)
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, f'aria-label="Needs me: {r.context["total_count"]} waiting"')
                # Each kind's filter link works too: ?kind= reruns the providers through the page
                for kind in {i.kind for i in r.context['items']}:
                    self.assertEqual(c.get(url, {'kind': kind}).status_code, 200, f'{email} ?kind={kind}')
