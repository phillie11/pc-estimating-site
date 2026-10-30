"""The drawing register: supersede rules, uploads with naming checks, filters, exports, code lists, migration."""
import csv
import io
from importlib import import_module

from django.apps import apps as global_apps
from django.test import TestCase

from documents.models import CodeList, RegisterSettings, code_lists
from projects.models import Document, DocumentRevision
from tenancy.context import org_context

from .base import DocBase, upload


class SupersedeTests(DocBase):
    def statuses(self, doc):
        with org_context(self.org_a):
            return {r.revision: (r.status, r.is_current, r.superseded_by.revision if r.superseded_by else None)
                    for r in DocumentRevision.objects.filter(document=doc).select_related('superseded_by')}

    def test_new_current_revision_supersedes_the_previous_one(self):
        d, _ = self.doc(revisions=('P01', 'P02', 'P03'), visibility=Document.VIS_SHARED)
        self.assertEqual(self.statuses(d), {'P01': ('SUPERSEDED', False, 'P02'), 'P02': ('SUPERSEDED', False, 'P03'),
                                            'P03': ('SHARED', True, None)})

    def test_older_revision_is_filed_as_superseded_by_the_current_one(self):
        d, _ = self.doc(revisions=('C01', 'P02'))
        self.assertEqual(self.statuses(d), {'C01': ('WIP', True, None), 'P02': ('SUPERSEDED', False, 'C01')})

    def test_status_follows_suitability(self):
        d, _ = self.doc(revisions=())
        with org_context(self.org_a):
            r = d.add_revision(DocumentRevision(revision='C01', suitability='A1', file=upload('c.pdf'),
                                                original_name='c.pdf'))
        self.assertEqual(r.status, DocumentRevision.PUBLISHED)

    def test_going_back_reinstates_and_supersedes_the_newer(self):
        d, revs = self.doc(revisions=('P01', 'P02'), visibility=Document.VIS_SHARED)
        with org_context(self.org_a):
            d.set_current(revs['P01'])
        self.assertEqual(self.statuses(d), {'P01': ('SHARED', True, None), 'P02': ('SUPERSEDED', False, 'P01')})

    def test_withdrawing_the_current_revision_hands_over_to_the_latest_left(self):
        d, revs = self.doc(revisions=('P01', 'P02'), visibility=Document.VIS_SHARED)
        with org_context(self.org_a):
            d.withdraw(revs['P02'], note='issued in error')
        s = self.statuses(d)
        self.assertEqual(s['P02'][:2], ('WITHDRAWN', False))
        self.assertEqual(s['P01'], ('SHARED', True, None))
        self.login(self.alice)
        r = self.client.get(self.purl('revision_detail', d.pk, revs['P02'].pk))
        self.assertContains(r, 'Withdrawn: do not use rev P02')

    def test_superseded_revision_stays_viewable_with_banner(self):
        d, revs = self.doc(revisions=('P01', 'P02', 'P03'))
        self.login(self.alice)
        r = self.client.get(self.purl('revision_detail', d.pk, revs['P01'].pk))
        self.assertContains(r, 'Superseded by P02')
        self.assertContains(r, 'The current revision is P03')
        r = self.client.get(self.purl('revision_detail', d.pk, revs['P02'].pk))
        self.assertContains(r, 'Superseded by P03')
        self.assertEqual(self.client.get(self.purl('document_download', d.pk, revs['P01'].pk)).status_code, 200)

    def test_withdraw_via_view_is_staff_only(self):
        d, revs = self.doc(revisions=('P01',), visibility=Document.VIS_SHARED)
        self.login(self.cli)
        r = self.client.post(self.purl('document_detail', d.pk), {'action': 'withdraw', 'revision': revs['P01'].pk})
        self.assertEqual(r.status_code, 403)
        self.login(self.staff)
        self.client.post(self.purl('document_detail', d.pk), {'action': 'withdraw', 'revision': revs['P01'].pk})
        self.assertEqual(self.fresh(revs['P01']).status, DocumentRevision.WITHDRAWN)


class UploadTests(DocBase):
    def post(self, **data):
        base = {'title': 'GA', 'reference': 'SLN-ABC-ZZ-02-DR-A-1001', 'category': 'DRAWING', 'visibility': 'INTERNAL',
                'file': upload('ga.pdf'), 'revision': 'P01'}
        return self.client.post(self.purl('document_list'), {**base, **data})

    def test_conforming_number_fills_the_fields(self):
        self.login(self.staff)
        r = self.post(suitability='S2')
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            d = Document.objects.get()
            rev = d.revisions.get()
        self.assertEqual((d.project_code, d.originator, d.volume, d.level, d.doc_type, d.role, d.number),
                         ('SLN', 'ABC', 'ZZ', '02', 'DR', 'A', '1001'))
        self.assertEqual((d.naming, d.discipline), (Document.NAMING_OK, 'Architect'))
        self.assertEqual((rev.suitability, rev.status, rev.page_count), ('S2', 'SHARED', 1))
        self.assertEqual(len(rev.sha256), 64)

    def test_number_with_warnings_needs_use_anyway(self):
        self.login(self.staff)
        r = self.post(reference='1203-A-100')
        self.assertContains(r, 'doesn&#x27;t follow ISO 19650')
        self.assertContains(r, 'Use this number anyway')
        with org_context(self.org_a):
            self.assertFalse(Document.objects.exists())
        r = self.post(reference='1203-A-100', accept_naming='on')
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            d = Document.objects.get()
        self.assertEqual((d.reference, d.naming), ('1203-A-100', Document.NAMING_ACCEPTED))

    def test_project_code_and_code_lists_are_checked(self):
        with org_context(self.org_a):
            RegisterSettings.objects.create(project=self.pa, project_code='ALP')
        self.login(self.staff)
        r = self.post()
        self.assertContains(r, 'match this project (ALP)')

    def test_naming_check_can_be_switched_off(self):
        with org_context(self.org_a):
            RegisterSettings.objects.create(project=self.pa, check_naming=False)
        self.login(self.staff)
        self.assertEqual(self.post(reference='whatever').status_code, 302)

    def test_external_upload_is_never_held_up_but_flagged(self):
        self.login(self.sub1)
        r = self.post(reference='RAMS-01', category='RAMS')
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            d = Document.objects.get()
        self.assertEqual((d.visibility, d.naming), (Document.VIS_SHARED, Document.NAMING_UNPARSED))
        self.login(self.staff)
        r = self.client.get(self.purl('document_list'))
        self.assertContains(r, "follow the project's naming")
        r = self.client.get(self.purl('document_list') + '?naming=check')
        self.assertContains(r, 'RAMS-01')

    def test_fixing_a_number_from_details(self):
        d, _ = self.doc(number='junk')
        self.assertEqual(d.naming, Document.NAMING_UNPARSED)
        self.login(self.staff)
        r = self.client.post(self.purl('document_detail', d.pk), {
            'action': 'details', 'title': 'GA', 'reference': 'SLN-ABC-ZZ-02-DR-A-1002', 'category': 'DRAWING',
            'discipline': '', 'sheet_size': 'A1', 'scale': '1:50', 'folder': 'Stage 4', 'visibility': 'INTERNAL'})
        self.assertEqual(r.status_code, 302)
        d = self.fresh(d)
        self.assertEqual((d.reference, d.naming, d.number, d.sheet_size), ('SLN-ABC-ZZ-02-DR-A-1002', 'OK', '1002', 'A1'))

    def test_revision_upload_on_shared_document_no_longer_notifies_everyone(self):
        from tenancy.models import Notification
        d, _ = self.doc(visibility=Document.VIS_SHARED)
        self.login(self.staff)
        self.client.post(self.purl('document_detail', d.pk), {'file': upload('p2.pdf'), 'revision': 'P02'})
        self.assertFalse(Notification.all_objects.filter(title__contains='SLN-ABC').exists())

    def test_issue_now_on_upload_sends_a_transmittal(self):
        from documents.models import Transmittal
        from tenancy.models import Notification
        d, _ = self.doc(visibility=Document.VIS_SHARED)
        self.login(self.staff)
        self.client.post(self.purl('document_detail', d.pk), {'file': upload('p2.pdf'), 'revision': 'P02',
                                                             'issue_now': 'on', 'purpose': 'CONSTRUCTION'})
        with org_context(self.org_a):
            tr = Transmittal.objects.get()
        self.assertEqual((tr.ref, tr.status, tr.purpose), ('TR-001', 'ISSUED', 'CONSTRUCTION'))
        for u in (self.cli, self.con, self.sub1, self.sub1b, self.sub2):
            self.assertTrue(Notification.all_objects.filter(user=u, title__startswith='TR-001').exists(), u)
        self.assertFalse(Notification.all_objects.filter(user=self.bidder).exists())


class RegisterPageTests(DocBase):
    def setUp(self):
        super().setUp()
        self.ga, _ = self.doc('SLN-ABC-ZZ-02-DR-A-1001', 'GA plan', ('P01', 'C01'), discipline='Architect')
        self.spec, _ = self.doc('SLN-MEP-ZZ-ZZ-SP-M-0001', 'Mechanical spec', ('P01',), discipline='Mechanical engineer')
        with org_context(self.org_a):
            self.spec.revisions.update(suitability='S3')
        self.login(self.staff)

    def get(self, query=''):
        return self.client.get(self.purl('document_list') + query)

    def test_filters(self):
        r = self.get('?discipline=architect')
        self.assertContains(r, 'GA plan')
        self.assertNotContains(r, 'Mechanical spec')
        r = self.get('?type=SP')
        self.assertContains(r, 'Mechanical spec')
        self.assertNotContains(r, 'GA plan')
        r = self.get('?suitability=S3')
        self.assertContains(r, 'Mechanical spec')
        self.assertNotContains(r, 'GA plan')
        r = self.get('?status=WIP&q=1001')
        self.assertContains(r, 'GA plan')
        self.assertNotContains(r, 'Mechanical spec')
        r = self.get('?status=PUBLISHED')
        self.assertContains(r, 'Nothing matches these filters')

    def test_empty_register_explains_next_step(self):
        r = self.client.get(self.url('document_list', self.pb.pk, org=self.org_b))
        self.assertEqual(r.status_code, 404)  # not Bravo's member
        self.login(self.bob)
        r = self.client.get(self.url('document_list', self.pb.pk, org=self.org_b))
        self.assertContains(r, 'The register is empty')
        self.assertContains(r, 'Bulk upload')

    def test_csv_export(self):
        r = self.get('?export=csv')
        self.assertEqual(r['Content-Type'], 'text/csv')
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[0][:3], ['Number', 'Title', 'Category'])
        ga = next(x for x in rows if x[0] == 'SLN-ABC-ZZ-02-DR-A-1001')
        self.assertEqual(ga[11], 'C01')
        self.assertEqual(len(rows), 3)

    def test_pdf_issue_sheet(self):
        r = self.get('?export=pdf&discipline=Architect')
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))


class CodeListTests(DocBase):
    def test_defaults_until_customised(self):
        with org_context(self.org_a):
            lists = code_lists(self.pa)
            self.assertIn(('DR', 'Drawing'), lists['doc_type'])
            self.assertIn(('A', 'Architect'), lists['role'])
            self.assertEqual([c for c, _ in lists['suitability']][:5], ['S0', 'S1', 'S2', 'S3', 'S4'])
            self.assertIn('CR', [c for c, _ in lists['suitability']])
            self.assertFalse(CodeList.objects.exists())

    def test_adding_a_code_copies_defaults_then_adds(self):
        self.login(self.staff)
        r = self.client.post(self.purl('register_settings'), {'action': 'add_code', 'field': 'doc_type',
                                                             'code': 'qq', 'description': 'Quirky type'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            codes = set(CodeList.objects.filter(project=self.pa, field='doc_type').values_list('code', flat=True))
        self.assertIn('QQ', codes)
        self.assertIn('DR', codes)
        # Now a QQ number passes and an unknown originator still doesn't matter (no list)
        r = self.client.post(self.purl('document_list'), {'title': 'X', 'reference': 'SLN-ANY-ZZ-02-QQ-A-1001',
                                                          'category': 'OTHER', 'visibility': 'INTERNAL',
                                                          'file': upload('x.pdf'), 'revision': 'P01'})
        self.assertEqual(r.status_code, 302)

    def test_removing_and_resetting(self):
        self.login(self.staff)
        self.client.post(self.purl('register_settings'), {'action': 'remove_code', 'field': 'role', 'code': 'A'})
        with org_context(self.org_a):
            self.assertNotIn('A', [c for c, _ in code_lists(self.pa)['role']])
        r = self.client.post(self.purl('document_list'), {'title': 'X', 'reference': 'SLN-ABC-ZZ-02-DR-A-1001',
                                                          'category': 'OTHER', 'visibility': 'INTERNAL',
                                                          'file': upload('x.pdf'), 'revision': 'P01'})
        self.assertContains(r, 'Role &quot;A&quot; isn&#x27;t in this project&#x27;s code list')
        self.client.post(self.purl('register_settings'), {'action': 'reset_codes', 'field': 'role'})
        with org_context(self.org_a):
            self.assertIn('A', [c for c, _ in code_lists(self.pa)['role']])

    def test_code_lists_are_per_project_and_company(self):
        with org_context(self.org_a):
            CodeList.objects.create(project=self.pa, field='originator', code='ABC')
        with org_context(self.org_b):
            self.assertFalse(CodeList.objects.exists())
            self.assertEqual(code_lists(self.pb)['originator'], [])

    def test_settings_validate_pattern_and_are_staff_only(self):
        self.login(self.staff)
        r = self.client.post(self.purl('register_settings'), {'action': 'settings', 'project_code': 'SLN',
                                                             'filename_pattern': '{bogus}', 'check_naming': 'on'})
        self.assertContains(r, 'Unknown part')
        for u in (self.cli, self.sub1):
            self.login(u)
            self.assertEqual(self.client.get(self.purl('register_settings')).status_code, 403)


class ReferenceMigrationTests(TestCase):
    """projects 0013 parses existing numbers best-effort and gives revisions their statuses, forward and back."""

    def test_forward_and_backward(self):
        from accounts.models import User
        from accounts.views import create_organization
        from projects.models import Project
        migration = import_module('projects.migrations.0013_document_register_data')
        u = User.objects.create_user(email='m@a.test', password='x', full_name='M')
        org = create_organization('Mig Co', u)
        org2 = create_organization('Other Co', User.objects.create_user(email='o@a.test', password='x'))
        made = {}
        for o in (org, org2):
            with org_context(o):
                p = Project.objects.create(name='P', stage=Project.STAGE_LIVE)
                iso = Document.objects.create(project=p, title='ISO', reference='sln-abc-zz-02-dr-a-1001',
                                              visibility='SHARED')
                odd = Document.objects.create(project=p, title='Odd', reference='1203-A-100', visibility='SHARED')
                bad = Document.objects.create(project=p, title='Bad', reference='SLN-ABC-ZZ-2-XX-Q-10A')
                blank = Document.objects.create(project=p, title='Photo')
                revs = []
                for code, current in (('P01', False), ('C01', True)):
                    revs.append(DocumentRevision.objects.create(document=iso, revision=code, is_current=current,
                                                                file=upload('a.pdf'), original_name='a.pdf',
                                                                status='X'))
                internal = DocumentRevision.objects.create(document=bad, revision='P01', is_current=True,
                                                           file=upload('b.pdf'), original_name='b.pdf', status='X')
                made[o.pk] = (iso, odd, bad, blank, revs, internal)
        # Start from "before": clear what the model filled in on save
        Document._base_manager.update(project_code='', originator='', volume='', level='', doc_type='', role='',
                                      number='', naming='', naming_notes='', discipline='')
        DocumentRevision._base_manager.update(status='', superseded_by=None)

        migration.fill(global_apps, None)
        iso, odd, bad, blank, revs, internal = made[org.pk]
        get = lambda m, pk: m._base_manager.get(pk=pk)  # noqa: E731
        d = get(Document, iso.pk)
        self.assertEqual((d.reference, d.doc_type, d.number, d.naming, d.discipline),
                         ('SLN-ABC-ZZ-02-DR-A-1001', 'DR', '1001', 'OK', 'Architect'))
        d = get(Document, odd.pk)
        self.assertEqual((d.reference, d.naming, d.number), ('1203-A-100', 'UNPARSED', ''))
        self.assertIn('couldn\'t be read', d.naming_notes)
        d = get(Document, bad.pk)
        self.assertEqual((d.naming, d.level), ('UNPARSED', '2'))
        self.assertIn('digits only', d.naming_notes)
        self.assertEqual(get(Document, blank.pk).naming, '')
        p01, c01 = (get(DocumentRevision, r.pk) for r in revs)
        self.assertEqual((p01.status, p01.superseded_by_id), ('SUPERSEDED', c01.pk))
        self.assertEqual(c01.status, 'PUBLISHED')
        self.assertEqual(get(DocumentRevision, internal.pk).status, 'WIP')
        # The other company's rows were done too, each against its own documents
        iso2 = get(Document, made[org2.pk][0].pk)
        self.assertEqual(iso2.naming, 'OK')
        self.assertEqual(get(DocumentRevision, made[org2.pk][4][0].pk).superseded_by_id, made[org2.pk][4][1].pk)

        migration.empty(global_apps, None)
        d = get(Document, iso.pk)
        self.assertEqual((d.naming, d.doc_type), ('', ''))
        self.assertEqual(get(DocumentRevision, revs[0].pk).status, '')
