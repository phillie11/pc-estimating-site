"""
Drawing viewer and pins: who can open which revision, the PDF stream's gates, which pins each
role sees, pin position checks, the create-form route, tenancy and the CSP.
"""
import json
import re
from decimal import Decimal
from unittest import mock

from django.contrib.staticfiles import finders
from django.utils import timezone
from django.test import SimpleTestCase, TestCase, override_settings

from documents.tests.base import DocBase, upload
from projects.models import Document, DocumentRevision, FileAccess, Issue, Project, ProjectMember
from rfis.models import RFI
from security.middleware import build_csp
from tenancy.context import TenantContextMissing, org_context
from viewer.models import DrawingPin, InvalidPin, parse_coord, parse_page


class CoordTests(SimpleTestCase):
    def test_accepts_numbers_from_0_to_1(self):
        for value, want in [('0', '0'), ('1', '1'), ('0.5', '0.5'), (0.25, '0.25'), ('0.1234567', '0.123457'),
                            (' 0.3 ', '0.3'), ('1.000000', '1')]:
            self.assertEqual(parse_coord(value), Decimal(want), value)

    def test_refuses_off_page_and_non_numbers(self):
        for value in ['1.5', '-0.1', '1.0000001', 'abc', '', None, 'NaN', 'nan', 'inf', '-Infinity', '1e5', True,
                      False, '0x1', '½', '[0.5]']:
            with self.assertRaises(InvalidPin, msg=repr(value)):
                parse_coord(value)

    def test_pages(self):
        self.assertEqual(parse_page('3'), 3)
        self.assertEqual(parse_page('2', page_count=2), 2)
        for value in ['0', '-1', 'x', '', None, '1.5', '٣', '10001']:
            with self.assertRaises(InvalidPin, msg=repr(value)):
                parse_page(value)
        with self.assertRaises(InvalidPin):
            parse_page('3', page_count=2)


class ViewerBase(DocBase):
    def setUp(self):
        super().setUp()
        # A shared drawing: P01 superseded by P02 (current); both shared
        self.ga, revs = self.doc(visibility=Document.VIS_SHARED, revisions=('P01', 'P02'))
        self.p01, self.p02 = revs['P01'], revs['P02']
        with org_context(self.org_a):
            DocumentRevision.objects.filter(pk__in=[self.p01.pk, self.p02.pk]).update(page_count=2)
            self.p01.refresh_from_db()
            self.p02.refresh_from_db()
            self.snag1 = Issue.objects.create(project=self.pa, title='Skirting', assignee=self.sub1, created_by=self.staff)
            self.snag2 = Issue.objects.create(project=self.pa, title='Sparks snag', assignee=self.sub2,
                                              created_by=self.staff)
            self.snag_staff = Issue.objects.create(project=self.pa, title='Staff snag', assignee=self.staff,
                                                   created_by=self.staff)
            self.rfi_shared = RFI(project=self.pa, title='Shared RFI', question='?')
            self.rfi_shared.save(actor=self.staff)
            self.rfi_internal = RFI(project=self.pa, title='Internal RFI', question='?', visibility=RFI.VIS_INTERNAL)
            self.rfi_internal.save(actor=self.staff)

    def pin(self, rev=None, issue=None, rfi=None, page=1, x='0.5', y='0.5', doc=None, by=None):
        rev = rev or self.p02
        with org_context(self.org_a):
            return DrawingPin.objects.create(project=self.pa, document=doc or self.ga, revision=rev,
                                             revision_code=rev.revision, page=page, x=Decimal(x), y=Decimal(y),
                                             issue=issue, rfi=rfi, created_by=by or self.staff)

    def viewer(self, rev=None, doc=None):
        rev = rev or self.p02
        return self.purl('drawing_viewer', (doc or self.ga).pk, rev.pk)

    def pdf(self, rev=None, doc=None):
        rev = rev or self.p02
        return self.purl('drawing_pdf', (doc or self.ga).pk, rev.pk)

    def post_pin(self, record, rev=None, page='1', x='0.5', y='0.5', doc=None):
        rev = rev or self.p02
        return self.client.post(self.purl('pin_create', (doc or self.ga).pk),
                                {'record': record, 'rev': rev.pk, 'page': page, 'x': x, 'y': y})

    def config(self, response):
        m = re.search(r'<script id="dv-config" type="application/json">(.*?)</script>', response.content.decode(), re.S)
        return json.loads(m.group(1))


class StreamTests(ViewerBase):
    """The PDF bytes: same people as a download, never withdrawn or work in progress for externals."""

    def setUp(self):
        super().setUp()
        # Work in progress on a shared document: S0, never shared
        self.wip_doc, revs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1002', title='WIP plan',
                                      visibility=Document.VIS_SHARED, revisions=())
        with org_context(self.org_a):
            self.wip = self.wip_doc.add_revision(DocumentRevision(revision='P01', suitability='S0', file=upload('w.pdf'),
                                                                  original_name='w.pdf', size=10,
                                                                  uploaded_by=self.alice))
        # A third revision on the GA, withdrawn: P02 becomes current again
        with org_context(self.org_a):
            self.p03 = self.ga.add_revision(DocumentRevision(revision='P03', file=upload('P03.pdf'),
                                                             original_name='P03.pdf', size=10, uploaded_by=self.alice))
            self.ga.withdraw(self.p03)
        self.internal_doc, irevs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1003', title='Internal plan')
        self.internal_rev = irevs['P01']

    def test_permission_matrix(self):
        ok, withdrawn = self.pdf(), self.pdf(self.p03)
        wip, internal = self.pdf(self.wip, self.wip_doc), self.pdf(self.internal_rev, self.internal_doc)
        matrix = {
            # user: {url: status}
            self.alice: {ok: 200, withdrawn: 200, wip: 200, internal: 200},
            self.admin: {ok: 200, withdrawn: 200, wip: 200, internal: 200},
            self.staff: {ok: 200, withdrawn: 200, wip: 200, internal: 200},
            self.cli: {ok: 200, withdrawn: 403, wip: 404, internal: 404},
            self.con: {ok: 200, withdrawn: 403, wip: 404, internal: 404},
            self.sub1: {ok: 200, withdrawn: 403, wip: 404, internal: 404},
            self.sub2: {ok: 200, withdrawn: 403, wip: 404, internal: 404},
            self.bidder: {ok: 403, withdrawn: 403, wip: 403, internal: 403},     # tender page only
            self.outsider: {ok: 404, withdrawn: 404, wip: 404, internal: 404},   # in the company, not on the job
            self.bob: {ok: 404, withdrawn: 404, wip: 404, internal: 404},        # another company
        }
        for user, urls in matrix.items():
            self.login(user)
            for url, want in urls.items():
                r = self.client.get(url)
                self.assertEqual(r.status_code, want, f'{user.email} {url}')
                if want == 200:
                    self.assertEqual(r['Content-Type'], 'application/pdf')
                    self.assertTrue(b''.join(r.streaming_content).startswith(b'%PDF'))
                    self.assertNotIn('attachment', r.get('Content-Disposition', ''))
            # The viewer page follows the same rules
            for url, want in urls.items():
                page = url.replace('/pdf/', '/viewer/')
                self.assertEqual(self.client.get(page).status_code, want, f'{user.email} {page}')
            self.client.logout()

    def test_external_on_another_job_is_refused(self):
        with org_context(self.org_a):
            other = Project.objects.create(name='Other job', stage=Project.STAGE_LIVE)
            ProjectMember.objects.create(project=other, user=self.sub2, role=ProjectMember.ROLE_SUBCONTRACTOR)
        d, revs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1009', visibility=Document.VIS_SHARED, project=other)
        self.login(self.cli)  # client on Alpha job only
        self.assertEqual(self.client.get(self.url('drawing_pdf', other.pk, d.pk, revs['P01'].pk)).status_code, 404)
        self.assertEqual(self.client.get(self.url('drawing_viewer', other.pk, d.pk, revs['P01'].pk)).status_code, 404)
        # And the Alpha job's document through the other job's URL
        self.login(self.sub2)
        self.assertEqual(self.client.get(self.url('drawing_pdf', other.pk, self.ga.pk, self.p02.pk)).status_code, 404)

    def test_viewing_is_logged_like_a_download(self):
        self.login(self.cli)
        self.client.get(self.pdf())
        with org_context(self.org_a):
            access = FileAccess.objects.get(user=self.cli)
        self.assertEqual(access.path, self.p02.file.name)
        self.assertIn('(viewer)', access.label)

    def test_scan_gate(self):
        from projects.models import FileScan
        self.login(self.cli)
        with mock.patch('projects.files.scanning_enabled', return_value=True), \
                mock.patch('projects.files.scan_status', return_value=None):
            self.assertEqual(self.client.get(self.pdf()).status_code, 409)
        with mock.patch('projects.files.scanning_enabled', return_value=True), \
                mock.patch('projects.files.scan_status', return_value=FileScan.INFECTED), \
                mock.patch('projects.files.alert_blocked'):
            self.assertEqual(self.client.get(self.pdf()).status_code, 403)

    @override_settings(STORAGE_BUCKET='siteline-test')
    def test_cloud_storage_streams_through_the_site(self):
        """The viewer fetches from script, and connect-src is 'self': no redirect to a signed S3 link."""
        self.login(self.cli)
        with mock.patch('projects.files.scanning_enabled', return_value=False):
            r = self.client.get(self.pdf())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(b''.join(r.streaming_content).startswith(b'%PDF'))

    def test_non_pdf_is_download_only(self):
        d, _ = self.doc(number='SLN-ABC-ZZ-02-DR-A-1004', revisions=())
        with org_context(self.org_a):
            rev = d.add_revision(DocumentRevision(revision='P01', file=upload('notes.docx', b'PK'),
                                                  original_name='notes.docx', size=2, uploaded_by=self.alice))
        self.login(self.staff)
        self.assertEqual(self.client.get(self.pdf(rev, d)).status_code, 404)
        r = self.client.get(self.viewer(rev, d))
        self.assertRedirects(r, self.purl('revision_detail', d.pk, rev.pk))

    def test_banners(self):
        self.login(self.staff)
        r = self.client.get(self.viewer(self.p03))
        self.assertContains(r, 'Withdrawn: do not use rev P03')
        self.assertNotContains(r, 'data-act="pin"')  # nothing pinned to a withdrawn revision
        r = self.client.get(self.viewer(self.p01))
        self.assertContains(r, 'Superseded by P02')
        self.assertContains(r, 'Open the current revision (P02)')
        r = self.client.get(self.viewer())
        for text in ('SLN-ABC-ZZ-02-DR-A-1001', '>P02<', 'Suitability', 'Shared'):
            self.assertContains(r, text)
        self.assertNotContains(r, 'not the current revision')
        self.assertNotContains(r, 'Superseded')


class PinVisibilityTests(ViewerBase):
    def setUp(self):
        super().setUp()
        self.pins = {
            'snag1': self.pin(issue=self.snag1),
            'snag2': self.pin(issue=self.snag2),
            'staff_snag': self.pin(issue=self.snag_staff),
            'rfi_shared': self.pin(rfi=self.rfi_shared),
            'rfi_internal': self.pin(rfi=self.rfi_internal),
        }

    def seen(self, user):
        self.login(user)
        r = self.client.get(self.viewer())
        self.assertEqual(r.status_code, 200)
        names = {v.pk: k for k, v in self.pins.items()}
        return {names[p['id']] for p in self.config(r)['pins']}

    def test_each_role_sees_only_its_records_pins(self):
        everything = set(self.pins)
        self.assertEqual(self.seen(self.alice), everything)
        self.assertEqual(self.seen(self.staff), everything)
        self.assertEqual(self.seen(self.sub1), {'snag1', 'rfi_shared'})
        self.assertEqual(self.seen(self.sub1b), {'rfi_shared'})  # same company, but not assigned to them
        self.assertEqual(self.seen(self.sub2), {'snag2', 'rfi_shared'})
        self.assertEqual(self.seen(self.cli), {'rfi_shared'})
        self.assertEqual(self.seen(self.con), {'rfi_shared'})

    def test_soft_deleted_rfi_pin_is_hidden(self):
        with org_context(self.org_a):
            RFI.all_objects.filter(pk=self.rfi_shared.pk).update(deleted_at=timezone.now())
        self.assertNotIn('rfi_shared', self.seen(self.staff))

    def test_pin_from_another_revision_is_marked(self):
        old = self.pin(rev=self.p01, issue=self.snag1, x='0.1', y='0.1')
        self.login(self.staff)
        pins = {p['id']: p for p in self.config(self.client.get(self.viewer()))['pins']}
        self.assertEqual(pins[old.pk]['placed_on'], 'placed on rev P01')
        self.assertEqual(pins[self.pins['snag2'].pk]['placed_on'], '')
        # Viewed on P01, the P02 pins are the "other" ones
        pins = {p['id']: p for p in self.config(self.client.get(self.viewer(self.p01)))['pins']}
        self.assertEqual(pins[self.pins['snag2'].pk]['placed_on'], 'placed on rev P02')

    def test_external_is_not_told_a_revision_number_they_were_never_issued(self):
        with org_context(self.org_a):
            wip = self.ga.add_revision(DocumentRevision(revision='P03', suitability='S0', file=upload('P03.pdf'),
                                                        original_name='P03.pdf', size=10, uploaded_by=self.alice))
        hidden = self.pin(rev=wip, rfi=self.rfi_shared)
        self.login(self.cli)
        pins = {p['id']: p for p in self.config(self.client.get(self.viewer()))['pins']}
        self.assertEqual(pins[hidden.pk]['placed_on'], 'placed on another revision')
        self.assertNotIn('P03', json.dumps(pins[hidden.pk]))

    def test_record_pages_link_to_the_drawing(self):
        open_url = self.purl('pin_open', self.pins['snag1'].pk)
        self.login(self.sub1)
        self.assertContains(self.client.get(self.purl('issue_detail', self.snag1.pk)), open_url)
        r = self.client.get(open_url)
        self.assertRedirects(r, self.viewer() + f'?pin={self.pins["snag1"].pk}')
        # Not their snag: no pin to open
        self.assertEqual(self.client.get(self.purl('pin_open', self.pins['snag2'].pk)).status_code, 404)
        self.login(self.staff)
        self.assertContains(self.client.get(self.purl('rfi_detail', self.rfi_shared.pk)),
                            self.purl('pin_open', self.pins['rfi_shared'].pk))

    def test_pin_on_a_document_the_external_cannot_see_stays_hidden(self):
        d, revs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1005', title='Internal drawing')
        internal_pin = self.pin(rev=revs['P01'], doc=d, rfi=self.rfi_shared)
        self.login(self.cli)
        r = self.client.get(self.purl('rfi_detail', self.rfi_shared.pk))
        self.assertNotContains(r, self.purl('pin_open', internal_pin.pk))
        self.assertContains(r, self.purl('pin_open', self.pins['rfi_shared'].pk))
        self.assertEqual(self.client.get(self.purl('pin_open', internal_pin.pk)).status_code, 404)

    def test_pin_open_goes_to_the_current_revision(self):
        old = self.pin(rev=self.p01, issue=self.snag_staff)
        self.login(self.staff)
        self.assertRedirects(self.client.get(self.purl('pin_open', old.pk)), self.viewer() + f'?pin={old.pk}')


class PinCreateTests(ViewerBase):
    def test_staff_pin_existing_snag_and_rfi(self):
        self.login(self.staff)
        r = self.post_pin(f'issue:{self.snag1.pk}', x='0.25', y='0.75', page='2')
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual((body['ref'], body['page'], body['x'], body['y']), (self.snag1.reference, 2, 0.25, 0.75))
        self.assertEqual(self.post_pin(f'rfi:{self.rfi_internal.pk}').status_code, 201)
        with org_context(self.org_a):
            pin = DrawingPin.objects.get(issue=self.snag1)
            self.assertEqual((pin.revision_id, pin.revision_code), (self.p02.pk, 'P02'))

    def test_pinning_again_moves_the_pin(self):
        self.login(self.staff)
        self.post_pin(f'issue:{self.snag1.pk}', x='0.1', y='0.1')
        self.post_pin(f'issue:{self.snag1.pk}', x='0.9', y='0.9')
        with org_context(self.org_a):
            self.assertEqual(DrawingPin.objects.filter(issue=self.snag1).count(), 1)
            self.assertEqual(DrawingPin.objects.get(issue=self.snag1).x, Decimal('0.9'))

    def test_coordinates_are_checked(self):
        self.login(self.staff)
        bad = [('1.5', '0.5'), ('0.5', '-0.1'), ('abc', '0.5'), ('', '0.5'), ('NaN', '0.5'), ('inf', '0.5'),
               ('0.5', '1e3'), ('0.5', 'Infinity')]
        for x, y in bad:
            r = self.post_pin(f'issue:{self.snag1.pk}', x=x, y=y)
            self.assertEqual(r.status_code, 400, (x, y))
            self.assertIn('error', r.json())
        for page in ('0', 'x', '3'):  # the revision has 2 pages
            self.assertEqual(self.post_pin(f'issue:{self.snag1.pk}', page=page).status_code, 400, page)
        r = self.client.post(self.purl('pin_create', self.ga.pk), {'record': f'issue:{self.snag1.pk}', 'rev': self.p02.pk,
                                                                   'page': '1', 'x': '0.5'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.post_pin('nonsense').status_code, 400)
        self.assertEqual(self.post_pin(f'issue:{self.snag1.pk}', x='0', y='1').status_code, 201)
        with org_context(self.org_a):
            self.assertEqual(DrawingPin.objects.count(), 1)

    def test_database_refuses_off_page_pins_too(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.pin(issue=self.snag1, x='1.5')
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.pin()  # no record

    def test_externals_pin_only_what_they_can_see(self):
        self.login(self.sub1)
        self.assertEqual(self.post_pin(f'issue:{self.snag1.pk}').status_code, 201)
        self.assertEqual(self.post_pin(f'issue:{self.snag2.pk}').status_code, 404)
        self.assertEqual(self.post_pin(f'issue:{self.snag_staff.pk}').status_code, 404)
        self.assertEqual(self.post_pin(f'rfi:{self.rfi_shared.pk}').status_code, 201)
        self.assertEqual(self.post_pin(f'rfi:{self.rfi_internal.pk}').status_code, 404)
        self.login(self.cli)
        self.assertEqual(self.post_pin(f'issue:{self.snag1.pk}').status_code, 404)
        for user, want in ((self.bidder, 403), (self.outsider, 404)):
            self.login(user)
            self.assertEqual(self.post_pin(f'rfi:{self.rfi_shared.pk}').status_code, want, user.email)
        self.login(self.bob)
        self.assertEqual(self.post_pin(f'rfi:{self.rfi_shared.pk}').status_code, 404)

    def test_no_pins_on_withdrawn_or_unshared_revisions(self):
        with org_context(self.org_a):
            p03 = self.ga.add_revision(DocumentRevision(revision='P03', suitability='S0', file=upload('P03.pdf'),
                                                        original_name='P03.pdf', size=10, uploaded_by=self.alice))
        self.login(self.cli)
        self.assertEqual(self.post_pin(f'rfi:{self.rfi_shared.pk}', rev=p03).status_code, 404)  # WIP
        with org_context(self.org_a):
            self.ga.withdraw(self.p01)
        self.login(self.staff)
        self.assertEqual(self.post_pin(f'rfi:{self.rfi_shared.pk}', rev=self.p01).status_code, 400)

    def test_get_is_not_allowed(self):
        self.login(self.staff)
        self.assertEqual(self.client.get(self.purl('pin_create', self.ga.pk)).status_code, 405)

    def test_archived_project_is_read_only(self):
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.viewer()).status_code, 200)
        self.assertEqual(self.post_pin(f'issue:{self.snag1.pk}').status_code, 403)

    def test_remove(self):
        mine = self.pin(rfi=self.rfi_shared, by=self.sub1)
        staffs = self.pin(issue=self.snag1)
        self.login(self.sub2)  # sees the RFI pin but didn't place it
        self.assertEqual(self.client.post(self.purl('pin_delete', self.ga.pk, mine.pk)).status_code, 403)
        self.login(self.sub1)
        self.assertEqual(self.client.post(self.purl('pin_delete', self.ga.pk, staffs.pk)).status_code, 403)
        self.assertEqual(self.client.post(self.purl('pin_delete', self.ga.pk, mine.pk)).status_code, 200)
        self.login(self.staff)
        self.assertEqual(self.client.post(self.purl('pin_delete', self.ga.pk, staffs.pk)).status_code, 200)
        with org_context(self.org_a):
            self.assertFalse(DrawingPin.objects.exists())


class CreateFormTests(ViewerBase):
    def params(self, **over):
        p = {'pin_doc': self.ga.pk, 'pin_rev': self.p02.pk, 'pin_page': 1, 'pin_x': '0.4', 'pin_y': '0.6'}
        p.update(over)
        return '?' + '&'.join(f'{k}={v}' for k, v in p.items())

    def test_new_snag_from_the_viewer(self):
        self.login(self.staff)
        url = self.purl('issue_create') + self.params()
        r = self.client.get(url)
        self.assertContains(r, 'This will be pinned to drawing')
        self.assertContains(r, 'value="Drawing SLN-ABC-ZZ-02-DR-A-1001 rev P02, page 1"')
        r = self.client.post(url, {'title': 'Cracked tile', 'issue_type': 'SNAG', 'priority': 'NORMAL',
                                   'location': 'Drawing SLN-ABC-ZZ-02-DR-A-1001 rev P02, page 1, by the bar'})
        with org_context(self.org_a):
            issue = Issue.objects.get(title='Cracked tile')
            pin = DrawingPin.objects.get(issue=issue)
        self.assertRedirects(r, self.viewer() + f'?pin={pin.pk}')
        self.assertEqual((pin.x, pin.y, pin.revision_code), (Decimal('0.4'), Decimal('0.6'), 'P02'))

    def test_new_rfi_by_the_client(self):
        self.login(self.cli)
        url = self.purl('rfi_create') + self.params()
        self.assertContains(self.client.get(url), 'value="SLN-ABC-ZZ-02-DR-A-1001 rev P02"')
        r = self.client.post(url, {'title': 'Which tile?', 'question': 'Please confirm', 'category': 'DESIGN',
                                   'priority': 'NORMAL', 'document_reference': 'SLN-ABC-ZZ-02-DR-A-1001 rev P02'})
        with org_context(self.org_a):
            rfi = RFI.objects.get(title='Which tile?')
            pin = DrawingPin.objects.get(rfi=rfi)
        self.assertTrue(rfi.is_shared)
        self.assertRedirects(r, self.viewer() + f'?pin={pin.pk}')

    def test_bad_or_unseen_pin_params_are_ignored(self):
        with org_context(self.org_a):
            wip = self.ga.add_revision(DocumentRevision(revision='P03', suitability='S0', file=upload('P03.pdf'),
                                                        original_name='P03.pdf', size=10, uploaded_by=self.alice))
        self.login(self.cli)
        for params in (self.params(pin_x='2'), self.params(pin_y='abc'), self.params(pin_rev=wip.pk),
                       self.params(pin_doc='x')):
            url = self.purl('rfi_create') + params
            self.assertNotContains(self.client.get(url), 'This will be pinned')
            self.client.post(url, {'title': 'Q', 'question': 'x', 'category': 'DESIGN', 'priority': 'NORMAL'})
        with org_context(self.org_a):
            self.assertFalse(DrawingPin.objects.exists())
            self.assertEqual(RFI.objects.filter(title='Q').count(), 4)

    def test_externals_still_cannot_raise_snags(self):
        self.login(self.sub1)
        self.assertEqual(self.client.get(self.purl('issue_create') + self.params()).status_code, 403)
        r = self.client.get(self.viewer())
        self.assertEqual(self.config(r)['new_snag_url'], '')
        self.assertNotEqual(self.config(r)['new_rfi_url'], '')


class EntryPointTests(ViewerBase):
    def test_view_buttons(self):
        with org_context(self.org_a):
            self.ga.withdraw(self.p02)  # P01 current again
        view_p01, view_p02 = self.viewer(self.p01), self.viewer(self.p02)
        self.login(self.staff)
        r = self.client.get(self.purl('document_detail', self.ga.pk))
        self.assertContains(r, view_p01)
        self.assertContains(r, view_p02)  # staff can view withdrawn, with the banner
        self.assertContains(self.client.get(self.purl('document_list')), view_p01)
        self.login(self.cli)
        r = self.client.get(self.purl('document_detail', self.ga.pk))
        self.assertContains(r, view_p01)
        self.assertNotContains(r, view_p02)
        self.assertContains(self.client.get(self.purl('document_list')), view_p01)
        self.assertContains(self.client.get(self.purl('revision_detail', self.ga.pk, self.p01.pk)), view_p01)


class IsolationTests(ViewerBase):
    def test_another_company_gets_404(self):
        pin = self.pin(issue=self.snag1)
        self.login(self.bob)
        for url in (self.url('drawing_viewer', self.pa.pk, self.ga.pk, self.p02.pk, org=self.org_b),
                    self.url('drawing_pdf', self.pa.pk, self.ga.pk, self.p02.pk, org=self.org_b),
                    self.url('pin_open', self.pa.pk, pin.pk, org=self.org_b),
                    self.viewer(), self.pdf(), self.purl('pin_open', pin.pk)):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        r = self.client.post(self.url('pin_delete', self.pa.pk, self.ga.pk, pin.pk, org=self.org_b))
        self.assertEqual(r.status_code, 404)
        with org_context(self.org_b):
            self.assertFalse(DrawingPin.objects.exists())
        with org_context(self.org_a):
            self.assertTrue(DrawingPin.objects.filter(pk=pin.pk).exists())

    def test_bravo_document_cannot_be_reached_through_alpha(self):
        bdoc, brevs = self.doc(number='SLN-ABC-ZZ-02-DR-A-1001', visibility=Document.VIS_SHARED, project=self.pb,
                               org=self.org_b)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.purl('drawing_pdf', bdoc.pk, brevs['P01'].pk)).status_code, 404)
        self.assertEqual(self.post_pin(f'issue:{self.snag1.pk}', rev=brevs['P01'], doc=bdoc).status_code, 404)


class FailClosedTests(TestCase):
    def test_no_org_context_fails_closed(self):
        with self.assertRaises(TenantContextMissing):
            list(DrawingPin.objects.all())


class CspTests(ViewerBase):
    def test_viewer_page_keeps_the_strict_csp(self):
        self.login(self.staff)
        r = self.client.get(self.viewer())
        csp = r['Content-Security-Policy']
        self.assertEqual(csp, build_csp())
        directives = dict(d.strip().split(' ', 1) for d in csp.split(';'))
        self.assertEqual(directives['script-src'], "'self'")
        self.assertEqual(directives['connect-src'], "'self'")
        self.assertNotIn('worker-src', directives)  # script-src 'self' covers the self-hosted worker
        self.assertNotIn('unsafe-eval', csp)
        self.assertNotIn('blob:', csp)
        for cdn in ('cdnjs', 'jsdelivr', 'unpkg', 'mozilla.github.io'):
            self.assertNotIn(cdn, csp)
        # Scripts on the page are all ours, with no inline code
        html = r.content.decode()
        for tag in re.findall(r'<script\b[^>]*>', html):
            self.assertTrue('src="/static/' in tag or 'application/json' in tag, tag)
        self.assertIn('/static/vendor/pdfjs/pdf.min.js', html)

    def test_pdfjs_is_self_hosted_pinned_and_licensed(self):
        for name in ('pdf.min.js', 'pdf.worker.min.js', 'LICENSE', 'README.txt',
                     'standard_fonts/LiberationSans-Regular.ttf', 'standard_fonts/LICENSE_FOXIT'):
            self.assertTrue(finders.find(f'vendor/pdfjs/{name}'), name)
        with open(finders.find('vendor/pdfjs/pdf.min.js'), encoding='utf-8') as f:
            self.assertIn('"4.10.38"', f.read())
        with open(finders.find('vendor/pdfjs/LICENSE'), encoding='utf-8') as f:
            self.assertIn('Apache License', f.read())
        with open(finders.find('viewer/viewer.js'), encoding='utf-8') as f:
            js = f.read()
        self.assertIn('isEvalSupported: false', js)
        self.assertNotIn('eval(', js)
        self.assertNotIn('http', js.split('*/', 1)[1])  # nothing fetched from another site
