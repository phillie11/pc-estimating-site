"""
Global search: each record type for each role finds exactly what that person could already open, and
nothing from another company. The fixtures put "Zebra" in one shared and one internal record of every
type, so a leak shows up as an extra hit.
"""
from django.db import connection
from django.test.utils import CaptureQueriesContext

from changes.tests.base import CommercialTestBase
from directory.models import Subcontractor
from help import search
from projects.models import Document, Project, ProjectMember
from rfis.models import RFI
from tenancy.context import TenantContextMissing, org_context
from variations.models import Variation


class SearchBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.bid = Project.objects.create(name='Zebra crossing bid', stage=Project.STAGE_LEAD)
            self.pa.name = 'Zebra House fit-out'
            self.pa.save()
            self.rfi_shared = RFI(project=self.pa, title='Zebra stripe finish', question='?', visibility=RFI.VIS_SHARED)
            self.rfi_shared.save(actor=self.pat)
            self.rfi_internal = RFI(project=self.pa, title='Zebra internal cost query', question='?',
                                    visibility=RFI.VIS_INTERNAL)
            self.rfi_internal.save(actor=self.pat)
            self.vo_draft = Variation(project=self.pa, title='Zebra draft change', description='x')
            self.vo_draft.save(actor=self.pat)
            self.vo_sent = Variation(project=self.pa, title='Zebra extra lighting', description='x',
                                     status=Variation.STATUS_SUBMITTED)
            self.vo_sent.save(actor=self.pat)
            self.doc_shared = Document.objects.create(project=self.pa, title='Zebra GA plan',
                                                      reference='SLN-ZEB-ZZ-00-DR-A-0001',
                                                      visibility=Document.VIS_SHARED)
            self.doc_internal = Document.objects.create(project=self.pa, title='Zebra internal sketch')
            self.bid_doc = Document.objects.create(project=self.bid, title='Zebra tender drawing',
                                                   visibility=Document.VIS_SHARED)
            self.zsub = Subcontractor.objects.create(company_name='Zebra Joinery Ltd')
        self.bob_zebra()

    def bob_zebra(self):
        """Another company with the same words everywhere: none of it may ever show up in Alpha."""
        with org_context(self.org_b):
            self.pb.name = 'Zebra Bravo job'
            self.pb.save()
            RFI(project=self.pb, title='Zebra Bravo question', question='?',
                visibility=RFI.VIS_SHARED).save(actor=self.bob)
            Document.objects.create(project=self.pb, title='Zebra Bravo drawing', visibility=Document.VIS_SHARED)
            Subcontractor.objects.create(company_name='Zebra Bravo Subbie')

    def found(self, user, q='zebra', org=None):
        self.client.logout()
        self.login(user)
        r = self.client.get(self.url('search', org=org) + '?' + 'q=' + q)
        self.assertEqual(r.status_code, 200)
        return {key: [h.title for h in hits] for key, _, hits in r.context['groups']}


class StaffSearchTests(SearchBase):
    def test_owner_finds_every_type_including_internal(self):
        got = self.found(self.alice)
        self.assertCountEqual(got['projects'], ['Zebra House fit-out', 'Zebra crossing bid'])
        self.assertCountEqual(got['rfis'], ['Zebra stripe finish', 'Zebra internal cost query'])
        self.assertCountEqual(got['variations'], ['Zebra draft change', 'Zebra extra lighting'])
        self.assertCountEqual(got['documents'], ['Zebra GA plan', 'Zebra internal sketch', 'Zebra tender drawing'])
        self.assertEqual(got['subcontractors'], ['Zebra Joinery Ltd'])
        self.assertNotIn('Bravo', str(got))

    def test_projects_team_sees_live_jobs_only(self):
        got = self.found(self.pat)
        self.assertEqual(got['projects'], ['Zebra House fit-out'])
        self.assertCountEqual(got['documents'], ['Zebra GA plan', 'Zebra internal sketch'])
        self.assertIn('subcontractors', got)

    def test_estimator_sees_bids_only(self):
        got = self.found(self.eve)
        self.assertEqual(got['projects'], ['Zebra crossing bid'])
        self.assertNotIn('rfis', got)
        self.assertNotIn('variations', got)
        self.assertEqual(got['documents'], ['Zebra tender drawing'])

    def test_people_are_the_workspace(self):
        got = self.found(self.alice, q='carla')
        self.assertEqual(got['people'], ['Carla Client'])
        self.assertEqual(self.found(self.alice, q='bob'), {})  # Bravo's owner isn't in Alpha

    def test_by_reference(self):
        got = self.found(self.pat, q='rfi-1')
        self.assertEqual(got['rfis'], ['Zebra stripe finish'])
        got = self.found(self.pat, q='VO 002')
        self.assertEqual(got['variations'], ['Zebra extra lighting'])
        got = self.found(self.pat, q=self.pa.reference)
        self.assertEqual(got['projects'], ['Zebra House fit-out'])
        got = self.found(self.pat, q='DR-A-0001')
        self.assertEqual(got['documents'], ['Zebra GA plan'])

    def test_archived_projects_for_owners_and_admins_only(self):
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.assertIn('Zebra House fit-out', self.found(self.adam)['projects'])
        self.assertNotIn('projects', self.found(self.pat))
        self.assertNotIn('rfis', self.found(self.pat))

    def test_archived_subcontractors_for_admins_only(self):
        with org_context(self.org_a):
            Subcontractor.objects.filter(pk=self.zsub.pk).update(archived_at=self.pa.created_at)
        self.assertEqual(self.found(self.adam)['subcontractors'], ['Zebra Joinery Ltd'])
        self.assertNotIn('subcontractors', self.found(self.pat))

    def test_short_or_empty_query_runs_nothing(self):
        self.login(self.alice)
        r = self.client.get(self.url('search') + '?q=z')
        self.assertContains(r, 'Type a little more')
        r = self.client.get(self.url('search'))
        self.assertContains(r, 'What are you looking for?')
        r = self.client.get(self.url('search') + '?q=nothing+like+this')
        self.assertContains(r, 'Nothing found')


class ExternalSearchTests(SearchBase):
    def test_client(self):
        got = self.found(self.carla)
        self.assertEqual(got['projects'], ['Zebra House fit-out'])
        self.assertEqual(got['rfis'], ['Zebra stripe finish'])
        self.assertEqual(got['variations'], ['Zebra extra lighting'])
        self.assertEqual(got['documents'], ['Zebra GA plan'])
        self.assertNotIn('subcontractors', got)
        self.assertNotIn('Bravo', str(got))

    def test_consultant(self):
        got = self.found(self.connie)
        self.assertEqual(got['variations'], ['Zebra extra lighting'])
        self.assertEqual(got['rfis'], ['Zebra stripe finish'])

    def test_subcontractor_sees_no_client_variations(self):
        got = self.found(self.sid)
        self.assertEqual(got['rfis'], ['Zebra stripe finish'])
        self.assertNotIn('variations', got)
        self.assertEqual(got['documents'], ['Zebra GA plan'])
        self.assertNotIn('subcontractors', got)

    def test_document_shared_with_other_roles_only(self):
        with org_context(self.org_a):
            Document.objects.filter(pk=self.doc_shared.pk).update(share_roles=',CLIENT,')
        self.assertEqual(self.found(self.carla)['documents'], ['Zebra GA plan'])
        self.assertNotIn('documents', self.found(self.sid))

    def test_tendering_sub_finds_only_their_tender(self):
        with org_context(self.org_a):
            ProjectMember.objects.filter(user=self.bea).update(project=self.bid)
        got = self.found(self.bea)
        self.assertEqual(got, {'projects': ['Zebra crossing bid']})  # it opens their tender page

    def test_people_on_their_projects_without_tendering_subs(self):
        got = self.found(self.carla, q='sid')
        self.assertEqual(got['people'], ['Sid Sub'])
        self.assertNotIn('people', self.found(self.carla, q='bea'))   # pricing the job: not shown to the client
        self.assertNotIn('people', self.found(self.carla, q='nora'))  # in the workspace, not on the project
        self.assertNotIn('people', self.found(self.carla, q='bob'))

    def test_external_not_on_any_project_finds_nothing(self):
        self.assertEqual(self.found(self.nora), {})


class IsolationTests(SearchBase):
    def test_other_company_member_gets_404_and_finds_only_their_own(self):
        self.login(self.bob)
        self.assertEqual(self.client.get(self.url('search') + '?q=zebra').status_code, 404)
        got = self.found(self.bob, org=self.org_b)
        self.assertEqual(got['projects'], ['Zebra Bravo job'])
        self.assertEqual(got['rfis'], ['Zebra Bravo question'])
        self.assertEqual(got['documents'], ['Zebra Bravo drawing'])
        self.assertEqual(got['subcontractors'], ['Zebra Bravo Subbie'])
        self.assertNotIn('Zebra House', str(got))
        self.assertNotIn('Joinery', str(got))

    def test_signed_out_is_sent_to_log_in(self):
        r = self.client.get(self.url('search') + '?q=zebra')
        self.assertEqual(r.status_code, 302)

    def test_no_organisation_fails_closed(self):
        request = self.request_for(self.alice)
        for key, _, fn in search.SOURCES:
            if key == 'people':
                continue  # Membership isn't a TenantModel: that query is pinned to request.org by hand
            with self.subTest(source=key), self.assertRaises(TenantContextMissing):
                fn(request, 'zebra', 5)


class SpeedTests(SearchBase):
    def test_query_count_is_fixed(self):
        for user in (self.alice, self.carla):
            self.client.logout()
            self.login(user)
            url = self.url('search') + '?q=zebra'
            self.client.get(url)
            with CaptureQueriesContext(connection) as small:
                self.client.get(url)
            with org_context(self.org_a):
                for i in range(15):
                    RFI(project=self.pa, title=f'Zebra extra {i}', question='?',
                        visibility=RFI.VIS_SHARED).save(actor=self.pat)
                    Document.objects.create(project=self.pa, title=f'Zebra sheet {i}', visibility=Document.VIS_SHARED)
                    Subcontractor.objects.create(company_name=f'Zebra Partitions {i}')
            with CaptureQueriesContext(connection) as big:
                self.client.get(url)
            self.assertEqual(len(big), len(small), user.email)
            self.assertLess(len(big), 50)
