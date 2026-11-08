"""
Close-out: practical completion and the defects period, defects (client reporting, the snag flow,
the defect phase), Needs me, the O&M manual through submittals and its compilation, certificates,
golden thread handover packs, who can open what for every role, other companies, archived jobs,
CSV exports and seed_demo.
"""
import hashlib
import io
import shutil
import tempfile
import zipfile
from datetime import date, timedelta
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

import actions
from core.workflow import apply_transition
from procurement.models import Submittal, SubmittalFile, SubmittalRevision
from projects.models import Document, DocumentRevision, FileAccess, Issue
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership, Notification

from .. import goldenthread, services
from ..actions import DefectsEndProvider, DefectToAssignProvider
from ..models import (Certificate, CompletionSection, HandoverItem, HandoverPack, OMCompilation, OMDocument, OMRequest,
                      OMSection, OMTemplateSection)
from .base import CloseoutTestBase, jpg, pdf


def kinds(test, user, kind, org=None):
    org = org or test.org_a
    with org_context(org):
        m = Membership.objects.filter(organization=org, user=user).first()
        return [i for i in actions.for_user(actions.request_for(user, m)) if i.kind == kind]


class CompletionTests(CloseoutTestBase):
    def test_recording_whole_works_pc_sets_the_contract_and_the_defects_end(self):
        self.login(self.pat)
        r = self.client.post(self.url('closeout_section', self.pa.pk, 'whole'), {'pc_date': '2026-08-31'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            s = CompletionSection.objects.get(project=self.pa, is_whole_works=True)
            self.assertEqual(s.defects_end, date(2027, 2, 28))  # the contract's 6 months
            self.contract.refresh_from_db()
        self.assertEqual(self.contract.practical_completion_on, date(2026, 8, 31))
        r = self.client.get(self.url('closeout_overview', self.pa.pk))
        self.assertContains(r, '28 Feb 2027')
        self.assertContains(r, 'Rectification Period')  # JCT words
        self.assertContains(r, 'Advisory')
        self.assertContains(r, 'Check your contract.')

    def test_a_section_can_have_its_own_defects_period(self):
        self.login(self.pat)
        self.client.post(self.url('closeout_section_add', self.pa.pk), {'name': 'Ground floor bar', 'pc_date': '2026-03-31'})
        with org_context(self.org_a):
            s = CompletionSection.objects.get(project=self.pa, name='Ground floor bar')
        self.assertFalse(s.is_whole_works)
        self.client.post(self.url('closeout_section', self.pa.pk, s.pk), {
            'name': 'Ground floor bar', 'pc_date': '2026-03-31', 'defects_period_months': '3'})
        self.assertEqual(self.fresh(s).defects_end, date(2026, 6, 30))
        with org_context(self.org_a):
            self.contract.refresh_from_db()
        self.assertIsNone(self.contract.practical_completion_on)  # only the whole works goes on the contract
        # Same name twice is refused; the whole works can't be removed, a section can
        r = self.client.post(self.url('closeout_section_add', self.pa.pk), {'name': 'ground floor bar'})
        self.assertContains(r, 'already a section')
        self.client.post(self.url('closeout_section', self.pa.pk, s.pk), {'action': 'delete'})
        with org_context(self.org_a):
            self.assertFalse(CompletionSection.objects.filter(pk=s.pk).exists())

    def test_made_good_before_pc_is_refused_with_how_to_fix_it(self):
        self.login(self.pat)
        r = self.client.post(self.url('closeout_section', self.pa.pk, 'whole'),
                             {'pc_date': '2026-06-01', 'made_good_on': '2026-05-01'})
        self.assertContains(r, 'before practical completion')
        r = self.client.post(self.url('closeout_section', self.pa.pk, 'whole'), {'made_good_on': '2026-05-01'})
        self.assertContains(r, 'Add the practical completion date first')

    def test_a_date_typed_on_the_contract_reaches_the_section(self):
        s = self.section(pc=date(2026, 6, 1))
        with org_context(self.org_a):
            self.contract.practical_completion_on = date(2026, 6, 5)
            self.contract.save()
        self.assertEqual(self.fresh(s).pc_date, date(2026, 6, 5))

    def test_whole_works_starts_from_the_contracts_date(self):
        with org_context(self.org_a):
            self.contract.practical_completion_on = date(2026, 6, 5)
            self.contract.save()
            self.assertEqual(services.whole_works(self.pa).pc_date, date(2026, 6, 5))

    def test_no_defects_period_anywhere_says_so(self):
        with org_context(self.org_a):
            self.contract.defects_period_months = None
            self.contract.save()
        s = self.section(pc=date(2026, 6, 1))
        with org_context(self.org_a):
            self.assertIsNone(self.fresh(s).defects_end)
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('closeout_overview', self.pa.pk)), 'The contract has no')


class DefectTests(CloseoutTestBase):
    def test_a_snag_raised_after_whole_works_pc_is_a_defect(self):
        self.assertEqual(self.issue().issue_type, 'SNAG')  # before PC
        self.section(pc=timezone.localdate())
        self.assertEqual(self.issue().issue_type, 'DEFECT')
        self.assertEqual(self.issue(issue_type='SAFETY').issue_type, 'SAFETY')  # other types are left alone
        self.section(pc=timezone.localdate(), whole=False, name='Bar')  # a section on its own changes nothing
        with org_context(self.org_a):
            CompletionSection.objects.filter(is_whole_works=True).update(pc_date=timezone.localdate() + timedelta(days=1))
        self.assertEqual(self.issue().issue_type, 'SNAG')

    def test_client_reports_a_defect_and_the_pm_hears(self):
        self.login(self.carla)
        r = self.client.get(self.url('closeout_client', self.pa.pk))
        self.assertContains(r, 'capture="environment"')
        self.assertContains(r, 'accept="image/*"')
        mail.outbox.clear()
        r = self.client.post(self.url('closeout_client', self.pa.pk), {
            'title': 'WC door does not close', 'location': 'First floor', 'photo': jpg()})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            i = Issue.objects.get(title='WC door does not close')
            self.assertTrue(Notification.objects.filter(user=self.pat, title__contains='Defect reported').exists())
        self.assertEqual((i.issue_type, i.created_by, i.assignee), ('DEFECT', self.carla, None))
        r = self.client.get(self.url('closeout_client', self.pa.pk))
        self.assertContains(r, 'WC door does not close')
        self.assertContains(r, 'Reported')
        # Only what and where are needed
        r = self.client.post(self.url('closeout_client', self.pa.pk), {'title': ''})
        self.assertContains(r, 'Say what is wrong')
        self.assertContains(r, 'Say where it is')

    def test_client_sees_only_client_side_defects(self):
        self.issue(title='Client one', issue_type='DEFECT', created_by=self.carla)
        self.issue(title='Consultant one', issue_type='DEFECT', created_by=self.connie)
        self.issue(title='Our internal one', issue_type='DEFECT', created_by=self.pat)
        self.issue(title='A snag', created_by=self.carla)
        self.login(self.carla)
        r = self.client.get(self.url('closeout_client', self.pa.pk))
        self.assertContains(r, 'Client one')
        self.assertContains(r, 'Consultant one')
        self.assertNotContains(r, 'Our internal one')
        self.assertNotContains(r, 'A snag')

    def test_sub_fixes_pm_checks_client_is_told(self):
        i = self.issue(title='Scratched panel', issue_type='DEFECT', created_by=self.carla, assignee=self.sid)
        self.assertEqual([x.title for x in kinds(self, self.sid, 'snag')], ['Scratched panel'])
        self.login(self.sid)
        self.client.post(self.url('issue_detail', self.pa.pk, i.pk), {'action': 'ready'})
        self.assertEqual([x.title for x in kinds(self, self.pat, 'snag_inspect')], ['Scratched panel'])
        self.login(self.pat)
        self.client.post(self.url('issue_detail', self.pa.pk, i.pk), {'action': 'close'})
        with org_context(self.org_a):
            self.assertTrue(Notification.objects.filter(user=self.carla, title__startswith='Defect fixed and checked')
                            .exists())
        self.login(self.carla)
        self.assertContains(self.client.get(self.url('closeout_client', self.pa.pk)), 'Fixed and checked')

    def test_staff_list_filters_search_and_csv(self):
        self.issue(title='Client lamp', issue_type='DEFECT', created_by=self.carla, location='Bar')
        self.issue(title='Our hinge', issue_type='DEFECT', created_by=self.pat, assignee=self.sid)
        self.issue(title='Closed one', issue_type='DEFECT', status=Issue.STATUS_CLOSED)
        self.issue(title='Not a defect')
        self.login(self.pat)
        url = self.url('closeout_defects', self.pa.pk)
        r = self.client.get(url)
        self.assertContains(r, 'Client lamp')
        self.assertContains(r, 'Our hinge')
        self.assertNotContains(r, 'Closed one')
        self.assertNotContains(r, 'Not a defect')
        self.assertNotContains(self.client.get(url + '?who=client'), 'Our hinge')
        self.assertNotContains(self.client.get(url + '?status=unassigned'), 'Our hinge')
        self.assertNotContains(self.client.get(url + '?q=lamp'), 'Our hinge')
        self.assertContains(self.client.get(url + '?status=all'), 'Closed one')
        r = self.client.get(url + '?export=csv&status=all')
        self.assertEqual(r['Content-Type'], 'text/csv')
        body = r.content.decode()
        self.assertIn('Client lamp', body)
        self.assertIn('Reported by', body)
        self.assertIn('Closed one', body)

    def test_csv_is_formula_safe(self):
        self.issue(title='  =HYPERLINK("http://evil.example","x")', issue_type='DEFECT', location='@SUM(A1)')
        self.login(self.pat)
        body = self.client.get(self.url('closeout_defects', self.pa.pk) + '?export=csv').content.decode()
        self.assertIn("'  =HYPERLINK", body)
        self.assertIn("'@SUM(A1)", body)

    def test_staff_report_a_defect_and_give_it_out(self):
        self.login(self.pat)
        r = self.client.post(self.url('closeout_defect_new', self.pa.pk), {
            'title': 'Loose skirting', 'location': 'Corridor', 'assignee': self.sid.pk, 'due_date': '2026-12-01'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            i = Issue.objects.get(title='Loose skirting')
            self.assertTrue(Notification.objects.filter(user=self.sid, title__startswith='Defect to fix').exists())
        self.assertEqual((i.issue_type, i.assignee, i.due_date), ('DEFECT', self.sid, date(2026, 12, 1)))

    def test_empty_list_explains_and_offers_the_first_step(self):
        self.login(self.pat)
        r = self.client.get(self.url('closeout_defects', self.pa.pk))
        self.assertContains(r, 'No defects yet')
        self.assertContains(r, 'Report a defect')


class NeedsMeTests(CloseoutTestBase):
    def test_end_of_defects_reminder_for_the_pm_six_weeks_out(self):
        today = timezone.localdate()
        from ..rules import add_months
        soon = self.section(pc=add_months(today, -6) + timedelta(days=30))  # ends in about 30 days
        self.section(pc=add_months(today, -6) + timedelta(days=60), whole=False, name='Later')  # not yet
        items = kinds(self, self.pat, 'defects_end')
        self.assertEqual(len(items), 1)
        with org_context(self.org_a):
            self.assertEqual(items[0].due, soon.defects_end)
        self.assertIn('Book the end-of-defects inspection', items[0].action)
        for other in (self.alice, self.adam, self.sid, self.carla, self.connie, self.eve):
            self.assertEqual(kinds(self, other, 'defects_end'), [], other)
        # Made good: it goes
        with org_context(self.org_a):
            CompletionSection.objects.filter(pk=soon.pk).update(made_good_on=today)
        self.assertEqual(kinds(self, self.pat, 'defects_end'), [])

    def test_ended_period_is_overdue_and_admins_get_jobs_with_no_pm(self):
        from ..rules import add_months
        self.section(pc=add_months(timezone.localdate(), -7))
        [item] = kinds(self, self.pat, 'defects_end')
        self.assertTrue(item.is_overdue)
        self.assertEqual(item.status_label, 'Defects period ended')
        with org_context(self.org_a):
            self.pa.manager = None
            self.pa.save()
        self.assertEqual(len(kinds(self, self.adam, 'defects_end')), 1)
        self.assertEqual(kinds(self, self.pat, 'defects_end'), [])

    def test_client_defects_to_give_out(self):
        self.issue(title='From client', issue_type='DEFECT', created_by=self.carla)
        self.issue(title='Given out', issue_type='DEFECT', created_by=self.carla, assignee=self.sid)
        self.issue(title='Ours', issue_type='DEFECT', created_by=self.pat)
        self.assertEqual([i.title for i in kinds(self, self.pat, 'defect_assign')], ['From client'])
        self.assertEqual(kinds(self, self.carla, 'defect_assign'), [])

    def test_providers_fail_closed_and_stay_in_their_company(self):
        from ..rules import add_months
        self.section(pc=add_months(timezone.localdate(), -6))
        req = self.request_for(self.pat)
        for provider in (DefectsEndProvider(), DefectToAssignProvider()):
            with self.assertRaises(TenantContextMissing):
                provider.items(req)
        with org_context(self.org_b):
            req_b = self.request_for(self.bob, org=self.org_b)
            self.assertEqual(DefectsEndProvider().items(req_b), [])
            self.assertEqual(DefectToAssignProvider().items(req_b), [])


class OMTests(CloseoutTestBase):
    def setup_manual(self):
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'setup'})
        with org_context(self.org_a):
            return {s.title: s for s in OMSection.objects.filter(project=self.pa)}

    def test_setup_copies_the_template_and_matches_trades(self):
        sections = self.setup_manual()
        self.assertEqual(sections['Joinery'].package, self.pkg)
        self.assertEqual(sections['Ceilings'].package, self.pkg2)
        self.assertIsNone(sections['Health and safety file'].package)
        with org_context(self.org_a):
            self.assertEqual(OMTemplateSection.objects.count(), len(sections))
        # Twice does nothing
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'setup'})
        with org_context(self.org_a):
            self.assertEqual(OMSection.objects.filter(project=self.pa).count(), len(sections))

    def test_request_goes_to_the_subs_portal_as_an_om_submittal(self):
        sections = self.setup_manual()
        r = self.client.get(self.url('closeout_om_request', self.pa.pk, sections['Joinery'].pk))
        self.assertContains(r, 'Send it by')
        r = self.client.post(self.url('closeout_om_request', self.pa.pk, sections['Joinery'].pk),
                             {'due_date': '2026-12-01', 'note': 'Bar joinery please'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            s = OMRequest.objects.get(section=sections['Joinery']).submittal
        self.assertEqual((s.kind, s.status, s.ball_in_court_company, s.due_date),
                         (Submittal.OM, Submittal.REQUIRED, self.subco, date(2026, 12, 1)))
        self.assertIn('Care and maintenance', s.description)
        self.assertEqual([i.title for i in kinds(self, self.sid, 'submittal')], ['O&M: Joinery'])
        self.assertEqual(kinds(self, self.sue, 'submittal'), [])
        r = self.client.get(self.url('closeout_om', self.pa.pk))
        self.assertContains(r, 'Waiting for information')
        # A section we supply can't be requested from anyone
        r = self.client.get(self.url('closeout_om_request', self.pa.pk, sections['Health and safety file'].pk))
        self.assertEqual(r.status_code, 302)

    def accepted(self, section):
        with org_context(self.org_a):
            s = services.request_om(section, self.pat, due_date=date(2026, 12, 1))
            rev = SubmittalRevision.objects.create(submittal=s, number=0, submitted_by=self.sid)
            SubmittalFile.objects.create(revision=rev, file=pdf('joinery-om.pdf', b'%PDF-1.4 joinery'),
                                         original_name='joinery-om.pdf', size=16)
            apply_transition(s, 'submit', self.sid)
            apply_transition(s, 'approve', self.pat)
        return s

    def test_compile_builds_an_indexed_zip_and_shares_it(self):
        sections = self.setup_manual()
        self.accepted(sections['Joinery'])
        self.client.post(self.url('closeout_om_upload', self.pa.pk, sections['Health and safety file'].pk),
                         {'title': 'Residual hazards', 'file': pdf('hs.pdf', b'%PDF-1.4 hs')})
        r = self.client.get(self.url('closeout_om', self.pa.pk))
        self.assertContains(r, 'Complete')
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'compile'})
        with org_context(self.org_a):
            comp = OMCompilation.objects.get(project=self.pa)
        self.assertEqual((comp.status, comp.number, comp.file_count), (OMCompilation.READY, 1, 2))
        r = self.client.get(self.url('closeout_om_download', self.pa.pk, comp.pk))
        data = b''.join(r.streaming_content)
        self.assertEqual(hashlib.sha256(data).hexdigest(), comp.sha256)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn('00 Contents and index.pdf', names)
        self.assertTrue(any(n.endswith('joinery-om.pdf') and 'Joinery' in n for n in names), names)
        self.assertTrue(any(n.endswith('Residual hazards.pdf') or n.endswith('hs.pdf') for n in names), names)
        # The client gets it only once shared
        self.login(self.carla)
        self.assertEqual(self.client.get(self.url('closeout_om_download', self.pa.pk, comp.pk)).status_code, 404)
        self.login(self.pat)
        self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'share', 'compilation': comp.pk})
        self.login(self.carla)
        self.assertContains(self.client.get(self.url('closeout_client', self.pa.pk)), 'O&amp;M manual, version 1')
        self.assertEqual(self.client.get(self.url('closeout_om_download', self.pa.pk, comp.pk)).status_code, 200)
        self.login(self.sid)
        self.assertEqual(self.client.get(self.url('closeout_om_download', self.pa.pk, comp.pk)).status_code, 403)

    def test_files_still_being_virus_checked_are_left_out_and_listed(self):
        sections = self.setup_manual()
        self.accepted(sections['Joinery'])
        with org_context(self.org_a), mock.patch('closeout.services.scan_status', return_value=None):
            comp = services.start_compilation(self.pa, self.pat)
        self.assertEqual(comp.file_count, 0)
        self.assertEqual(comp.left_out[0][2], 'Waiting for virus check')

    def test_big_manuals_go_to_the_worker(self):
        sections = self.setup_manual()
        self.accepted(sections['Joinery'])
        with org_context(self.org_a), mock.patch('closeout.services.INLINE_MAX_FILES', 0):
            comp = services.start_compilation(self.pa, self.pat)
            self.assertEqual(comp.status, OMCompilation.QUEUED)
            from closeout.tasks import compile_om
            compile_om({'id': comp.pk})
            compile_om({'id': comp.pk})  # a second run changes nothing
            comp.refresh_from_db()
        self.assertEqual((comp.status, comp.file_count), (OMCompilation.READY, 1))

    def test_csv_tracker_and_template_page(self):
        self.setup_manual()
        r = self.client.get(self.url('closeout_om', self.pa.pk) + '?export=csv')
        self.assertEqual(r['Content-Type'], 'text/csv')
        self.assertIn('Joinery', r.content.decode())
        self.login(self.adam)
        self.assertContains(self.client.get(self.url('closeout_om_template')), 'O&amp;M manual contents')
        for user in (self.pat, self.carla, self.sid):
            self.assertEqual(self.status_for(user, self.url('closeout_om_template')), 403, user)
        self.assertEqual(self.status_for(self.bob, self.url('closeout_om_template')), 404)

    def test_template_edits(self):
        self.login(self.adam)
        url = self.url('closeout_om_template')
        self.client.get(url)
        with org_context(self.org_a):
            rows = list(OMTemplateSection.objects.all())
        data = {'form-TOTAL_FORMS': str(len(rows) + 1), 'form-INITIAL_FORMS': str(len(rows)),
                'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000'}
        for n, row in enumerate(rows):
            data.update({f'form-{n}-id': row.pk, f'form-{n}-title': row.title, f'form-{n}-trade': row.trade,
                         f'form-{n}-contents': row.contents, f'form-{n}-order': row.order})
        data['form-0-DELETE'] = 'on'
        n = len(rows)
        data.update({f'form-{n}-title': 'Signage', f'form-{n}-trade': 'Signs', f'form-{n}-contents': 'Sign schedule',
                     f'form-{n}-order': '200'})
        self.assertEqual(self.client.post(url, data).status_code, 302)
        with org_context(self.org_a):
            titles = list(OMTemplateSection.objects.values_list('title', flat=True))
        self.assertIn('Signage', titles)
        self.assertNotIn(rows[0].title, titles)


class CertificateTests(CloseoutTestBase):
    def test_upload_list_download_logged(self):
        self.login(self.pat)
        r = self.client.post(self.url('closeout_certificates', self.pa.pk),
                             {'kind': 'ELECTRICAL', 'file': pdf('eic.pdf'), 'issued_by': 'Sparks Ltd'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            cert = Certificate.objects.get()
        r = self.client.get(self.url('closeout_certificates', self.pa.pk))
        self.assertContains(r, 'Electrical installation certificate')
        r = self.client.get(self.url('closeout_certificate_file', self.pa.pk, cert.pk))
        self.assertEqual(r.status_code, 200)
        with org_context(self.org_a):
            self.assertTrue(FileAccess.objects.filter(label__contains='Electrical installation').exists())
        self.assertNotContains(self.client.get(self.url('closeout_certificates', self.pa.pk) + '?kind=GAS'), 'eic.pdf')

    def test_blocked_extensions_and_empty_state(self):
        self.login(self.pat)
        r = self.client.get(self.url('closeout_certificates', self.pa.pk))
        self.assertContains(r, 'No certificates yet')
        r = self.client.post(self.url('closeout_certificates', self.pa.pk), {
            'kind': 'OTHER', 'file': pdf('bad.exe')})
        self.assertContains(r, '.exe files are not allowed')


class PackTests(CloseoutTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            doc = Document.objects.create(project=self.pa, title='GA plan', reference='GA-01', category='DRAWING',
                                          visibility=Document.VIS_SHARED)  # only drawings given to the client go in
            doc.add_revision(DocumentRevision(revision='P01', file=pdf('ga-p01.pdf', b'%PDF-1.4 p01'),
                                              original_name='ga-p01.pdf', size=12))
            doc.add_revision(DocumentRevision(revision='C01', file=pdf('ga-c01.pdf', b'%PDF-1.4 c01'),
                                              original_name='ga-c01.pdf', size=12))
            spec = Document.objects.create(project=self.pa, title='Spec', category='SPEC')
            spec.add_revision(DocumentRevision(revision='P01', file=pdf('spec.pdf'), original_name='spec.pdf'))
            Certificate.objects.create(project=self.pa, kind='FIRE_ALARM', file=pdf('fa.pdf', b'%PDF-1.4 fire alarm'),
                                       original_name='fa.pdf', size=19)

    def build(self):
        self.login(self.pat)
        self.client.post(self.url('closeout_packs', self.pa.pk), {'note': 'Bar handover'})
        with org_context(self.org_a):
            return HandoverPack.objects.get(project=self.pa)

    def test_pack_holds_current_drawings_and_certificates_with_checksums(self):
        pack = self.build()
        self.assertEqual(pack.status, HandoverPack.READY)
        with org_context(self.org_a):
            items = {it.title: it for it in pack.items.all()}
            self.assertEqual(set(items), {'GA plan', 'Fire alarm certificate'})  # current revision only, no specs
            ga = items['GA plan']
            self.assertEqual((ga.revision, ga.category, ga.state), ('C01', HandoverItem.DRAWING, HandoverItem.OK))
            self.assertEqual(ga.sha256, hashlib.sha256(b'%PDF-1.4 c01').hexdigest())
            self.assertEqual(items['Fire alarm certificate'].sha256, hashlib.sha256(b'%PDF-1.4 fire alarm').hexdigest())
            self.assertEqual(pack.manifest_sha256, services.manifest_checksum(pack))
        r = self.client.get(pack.get_absolute_url())
        self.assertContains(r, ga.sha256)
        self.assertContains(r, 'Building Safety Act')
        r = self.client.get(pack.get_absolute_url() + '?export=csv')
        self.assertIn(ga.sha256, r.content.decode())
        r = self.client.get(self.url('closeout_pack_manifest', self.pa.pk, pack.pk))
        self.assertEqual(r['Content-Type'], 'application/pdf')

    def test_client_sees_only_issued_packs_and_downloads_are_logged(self):
        pack = self.build()
        with org_context(self.org_a):
            item = pack.items.get(title='GA plan')
        self.login(self.carla)
        self.assertEqual(self.client.get(self.url('closeout_pack_item', self.pa.pk, pack.pk, item.pk)).status_code, 404)
        self.assertNotContains(self.client.get(self.url('closeout_client', self.pa.pk)), 'Handover pack 1')
        self.login(self.pat)
        self.client.post(pack.get_absolute_url(), {'action': 'issue'})
        self.assertEqual(self.fresh(pack).status, HandoverPack.ISSUED)
        with org_context(self.org_a):
            self.assertTrue(Notification.objects.filter(user=self.carla, title__startswith='Handover pack 1').exists())
        self.login(self.carla)
        r = self.client.get(self.url('closeout_client', self.pa.pk))
        self.assertContains(r, 'Handover pack 1')
        self.assertContains(r, item.sha256)
        r = self.client.get(self.url('closeout_pack_item', self.pa.pk, pack.pk, item.pk))
        self.assertEqual(b''.join(r.streaming_content), b'%PDF-1.4 c01')
        with org_context(self.org_a):
            self.assertTrue(FileAccess.objects.filter(user=self.carla, label__contains='Handover pack 1').exists())
        self.assertEqual(self.client.get(self.url('closeout_pack_manifest', self.pa.pk, pack.pk)).status_code, 200)
        # Clients never get the staff pack page
        self.assertEqual(self.client.get(pack.get_absolute_url()).status_code, 403)
        self.login(self.sid)
        self.assertEqual(self.client.get(self.url('closeout_pack_item', self.pa.pk, pack.pk, item.pk)).status_code, 403)

    def test_the_pack_is_frozen_when_the_drawing_moves_on(self):
        pack = self.build()
        with org_context(self.org_a):
            doc = Document.objects.get(reference='GA-01')
            doc.add_revision(DocumentRevision(revision='C02', file=pdf('ga-c02.pdf', b'%PDF-1.4 c02'),
                                              original_name='ga-c02.pdf'))
            item = pack.items.get(title='GA plan')
        self.assertEqual(item.revision, 'C01')
        r = self.client.get(self.url('closeout_pack_item', self.pa.pk, pack.pk, item.pk))
        self.assertEqual(b''.join(r.streaming_content), b'%PDF-1.4 c01')

    def test_files_waiting_for_the_virus_check_hold_the_pack_and_are_not_served(self):
        with mock.patch('closeout.services.scan_status', return_value=None):
            pack = self.build()
        self.assertEqual(pack.status, HandoverPack.BUILDING)
        with org_context(self.org_a):
            item = pack.items.get(title='GA plan')
        self.assertEqual(item.state, HandoverItem.WAITING)
        self.assertEqual(self.client.get(self.url('closeout_pack_item', self.pa.pk, pack.pk, item.pk)).status_code, 404)
        self.client.post(pack.get_absolute_url(), {'action': 'recheck'})
        self.assertEqual(self.fresh(pack).status, HandoverPack.READY)
        with org_context(self.org_a), mock.patch('closeout.services.scan_status', return_value='THREATS_FOUND'):
            pack2 = services.build_pack(self.pa, self.pat)
            self.assertEqual(pack2.items.get(title='GA plan').state, HandoverItem.BLOCKED)

    def test_scan_gate_applies_to_pack_downloads(self):
        pack = self.build()
        with org_context(self.org_a):
            item = pack.items.get(title='GA plan')
        with mock.patch('projects.files._scan_gate') as gate:
            from django.http import HttpResponse
            gate.return_value = HttpResponse('blocked', status=403)
            r = self.client.get(self.url('closeout_pack_item', self.pa.pk, pack.pk, item.pk))
        self.assertEqual(r.status_code, 403)
        gate.assert_called_once()

    def test_quality_is_optional(self):
        with mock.patch('closeout.goldenthread.quality_installed', return_value=False):
            with org_context(self.org_a):
                self.assertEqual(goldenthread.quality_items(self.pa), [])
            self.login(self.pat)
            self.assertContains(self.client.get(self.url('closeout_packs', self.pa.pk)), "isn't switched on here yet")

    def test_no_fire_stopping_recorded_adds_no_register(self):
        with org_context(self.org_a):
            self.assertEqual(goldenthread.quality_items(self.pa), [])

    def test_om_and_completion_certificates_go_in(self):
        with org_context(self.org_a):
            s = CompletionSection(project=self.pa, is_whole_works=True, name=CompletionSection.WHOLE_WORKS,
                                  pc_date=date(2026, 6, 1))
            s.certificate.save('pc.pdf', pdf('pc.pdf'), save=True)
            sec = OMSection.objects.create(project=self.pa, title='H&S file')
            OMDocument.objects.create(section=sec, title='Hazards', file=pdf('hz.pdf'), original_name='hz.pdf')
            cats = [src.category for src in goldenthread.collect(self.pa)]
        self.assertEqual(cats, [HandoverItem.COMPLETION, HandoverItem.DRAWING, HandoverItem.OM, HandoverItem.CERTIFICATE])


class AccessTests(CloseoutTestBase):
    """Every page, every role: staff on the job, the estimator, client, consultant, subs, bidder, outsider."""

    def setUp(self):
        super().setUp()
        self.sec = self.section(pc=date(2026, 6, 1), whole=False, name='Bar')
        with org_context(self.org_a):
            self.om = OMSection.objects.create(project=self.pa, title='Joinery', package=self.pkg)
            self.doc = OMDocument.objects.create(section=self.om, title='Data', file=pdf(), original_name='d.pdf')
            self.cert = Certificate.objects.create(project=self.pa, kind='GAS', file=pdf(), original_name='g.pdf')

    def staff_urls(self):
        p = self.pa.pk
        return [self.url('closeout_overview', p), self.url('closeout_section', p, 'whole'),
                self.url('closeout_section', p, self.sec.pk), self.url('closeout_section_add', p),
                self.url('closeout_defects', p), self.url('closeout_defect_new', p), self.url('closeout_om', p),
                self.url('closeout_om_section', p, self.om.pk), self.url('closeout_om_section_new', p),
                self.url('closeout_om_request', p, self.om.pk), self.url('closeout_om_upload', p, self.om.pk),
                self.url('closeout_om_document', p, self.doc.pk), self.url('closeout_certificates', p),
                self.url('closeout_certificate_file', p, self.cert.pk), self.url('closeout_packs', p)]

    def test_staff_on_the_job_open_everything(self):
        for user in (self.alice, self.adam, self.pat):
            for url in self.staff_urls():
                self.assertEqual(self.status_for(user, url), 200, (user, url))

    def test_externals_and_outsiders_are_refused(self):
        expected = {'eve': 404, 'nora': 404, 'bob': 404, 'sid': 403, 'sue': 403, 'bea': 403, 'connie': 403, 'carla': 403}
        for name, code in expected.items():
            user = getattr(self, name)
            for url in self.staff_urls()[1:]:
                self.assertEqual(self.status_for(user, url), code, (name, url))
        # The overview sends the client side to their own page
        self.assertEqual(self.status_for(self.carla, self.url('closeout_overview', self.pa.pk)), 302)

    def test_client_page_is_for_the_client_side_only(self):
        url = self.url('closeout_client', self.pa.pk)
        self.assertEqual(self.status_for(self.carla, url), 200)
        self.assertEqual(self.status_for(self.connie, url), 200)
        self.assertEqual(self.status_for(self.pat, url), 302)  # staff go to the Close-out tab
        for user, code in ((self.sid, 403), (self.sue, 403), (self.bea, 403), (self.nora, 404), (self.bob, 404),
                           (self.eve, 404)):
            self.assertEqual(self.status_for(user, url), code, user)
            self.assertEqual(self.status_for(user, url, 'post', {'title': 'x', 'location': 'y'}), code, user)

    def test_tabs(self):
        self.login(self.pat)
        r = self.client.get(self.url('project_detail', self.pa.pk))
        self.assertContains(r, 'Close-out')
        self.login(self.carla)
        r = self.client.get(self.url('closeout_client', self.pa.pk))
        self.assertContains(r, 'Defects and handover')
        self.login(self.sid)
        self.assertNotContains(self.client.get(self.url('project_detail', self.pa.pk)), 'Defects and handover')

    def test_no_money_on_the_client_page(self):
        self.login(self.carla)
        self.assertNotContains(self.client.get(self.url('closeout_client', self.pa.pk)), '£')

    def test_archived_job_is_read_only(self):
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.url('closeout_overview', self.pa.pk)).status_code, 200)
        r = self.client.post(self.url('closeout_section', self.pa.pk, 'whole'), {'pc_date': '2026-07-01'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.post(self.url('closeout_om', self.pa.pk), {'action': 'setup'}).status_code, 403)


class TenancyTests(CloseoutTestBase):
    def test_another_company_gets_a_404(self):
        sec = self.section()
        with org_context(self.org_a):
            om = OMSection.objects.create(project=self.pa, title='Joinery')
            pack = HandoverPack.objects.create(project=self.pa, status=HandoverPack.ISSUED)
        p = self.pa.pk
        for url in (self.url('closeout_overview', p), self.url('closeout_section', p, sec.pk),
                    self.url('closeout_om_section', p, om.pk), self.url('closeout_pack', p, pack.pk),
                    self.url('closeout_client', p), self.url('closeout_defects', p)):
            self.assertEqual(self.status_for(self.bob, url), 404, url)
        with org_context(self.org_b):
            Membership.objects.create(organization=self.org_b, user=self.pat, role=Membership.ROLE_MEMBER)
        for name, args in (('closeout_section', (sec.pk,)), ('closeout_om_section', (om.pk,)),
                           ('closeout_pack', (pack.pk,)), ('closeout_pack_manifest', (pack.pk,))):
            self.assertEqual(self.status_for(self.pat, self.url(name, self.pb.pk, *args, org=self.org_b)), 404, name)

    def test_no_organisation_fails_closed(self):
        self.section()
        for model in (CompletionSection, OMTemplateSection, OMSection, OMRequest, OMDocument, OMCompilation,
                      Certificate, HandoverPack, HandoverItem):
            with self.assertRaises(TenantContextMissing):
                model.objects.count()

    def test_rows_stay_in_their_company(self):
        self.section()
        with org_context(self.org_b):
            self.assertEqual(CompletionSection.objects.count(), 0)
            OMTemplateSection.for_org(self.org_b)
        with org_context(self.org_a):
            self.assertEqual(OMTemplateSection.objects.count(), 0)


class SeedDemoTests(TestCase):
    def test_demo_has_close_out_on_leeds_salford_and_a_completed_job(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            call_command('seed_demo', stdout=io.StringIO())
        from accounts.models import User
        from projects.models import Project
        from tenancy.models import Organization
        org = Organization.objects.get(slug='northbank-interiors')
        with org_context(org):
            leeds = Project.objects.get(name='Bar & kitchen fit-out, Call Lane')
            bar = CompletionSection.objects.get(project=leeds, name='Ground floor bar')
            self.assertEqual(bar.defects_end, services.rules.defects_end(bar.pc_date, 12))  # the contract's default
            self.assertEqual(Issue.objects.filter(project=leeds, issue_type='DEFECT').count(), 3)
            self.assertTrue(OMSection.objects.filter(project__name='Office CAT A, Salford Quays').exists())
            comp = OMCompilation.objects.get(project=leeds)
            self.assertTrue(comp.shared_with_client and comp.status == OMCompilation.READY)
            self.assertEqual(HandoverPack.objects.get(project=leeds).status, HandoverPack.ISSUED)

            def items(email, kind):
                user = User.objects.get(email=email)
                m = Membership.objects.get(organization=org, user=user)
                return [i for i in actions.for_user(actions.request_for(user, m)) if i.kind == kind]
            self.assertEqual(len(items('pm@demo.test', 'defect_assign')), 1)
            self.assertEqual(len(items('pm@demo.test', 'defects_end')), 1)
            self.assertGreaterEqual(len(items('pm@demo.test', 'snag_inspect')), 1)
            self.assertTrue(any(i.title == 'Scratch to bar front veneer panel' for i in items('sub@demo.test', 'snag')))
            self.assertEqual(Submittal.objects.get(title='O&M: Partitions and linings').status, Submittal.REQUIRED)
            self.assertEqual(Submittal.objects.get(title='O&M: Joinery').status, Submittal.APPROVED)
