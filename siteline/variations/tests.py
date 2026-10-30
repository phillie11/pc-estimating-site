"""
Client variations on the record base: numbering, index, audit, transitions and who may make
them (only the client approves), visibility, the RFI link, and tenancy.
"""
from decimal import Decimal

from datetime import timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from changes.links import came_from
from changes.tests.base import CommercialTestBase
from contracts.models import MainContract
from core.access import needs_me, visible_index
from core.exceptions import TransitionForbidden
from core.models import AuditEntry, RecordIndex
from core.workflow import apply_transition
from rfis.models import RFI
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Notification

from .models import Variation


class VariationTestBase(CommercialTestBase):
    def vo(self, project=None, org=None, actor=None, **kw):
        kw.setdefault('title', 'Extra ceiling')
        kw.setdefault('description', 'x')
        kw.setdefault('value', Decimal('1000.00'))
        with org_context(org or self.org_a):
            v = Variation(project=project or self.pa, **kw)
            v.save(actor=actor or self.pat)
            return v

    def submitted(self, **kw):
        v = self.vo(**kw)
        with org_context(self.org_a):
            apply_transition(v, 'submit', self.pat)
        return v

    def detail(self, v):
        return self.url('variation_detail', self.pa.pk, v.pk)


class RecordTests(VariationTestBase):
    def test_numbering_ref_and_index(self):
        a, b = self.vo(), self.vo()
        other = self.vo(project=self.pb, org=self.org_b, actor=self.bob)
        self.assertEqual((a.ref, b.ref, other.ref), ('VO-001', 'VO-002', 'VO-001'))
        with org_context(self.org_a):
            idx = RecordIndex.objects.get(ref='VO-001', project=self.pa)
            self.assertEqual((idx.record_type, idx.status, idx.visibility, idx.is_open),
                             ('variations.variation', 'DRAFT', 'INTERNAL', False))
            self.assertEqual(a.created_by, self.pat)
            self.assertEqual(AuditEntry.objects.filter(record_idx=idx).first().action, 'created')

    def test_drafts_are_internal_and_hidden_from_the_client(self):
        draft = self.vo(title='Secret draft')
        self.assertEqual(draft.visibility, Variation.VIS_INTERNAL)
        self.login(self.carla)
        self.assertNotContains(self.client.get(self.url('variation_list', self.pa.pk)), 'Secret draft')
        self.assertEqual(self.client.get(self.detail(draft)).status_code, 404)

    def test_withdrawn_draft_stays_internal(self):
        v = self.vo()
        with org_context(self.org_a):
            apply_transition(v, 'withdraw', self.pat)
        self.assertEqual((v.status, v.visibility), ('WITHDRAWN', 'INTERNAL'))


class TransitionTests(VariationTestBase):
    def test_submit_shares_it_and_passes_the_ball_to_the_client(self):
        v = self.vo()
        self.login(self.pat)
        r = self.client.post(self.detail(v), {'action': 'submit'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            v = Variation.objects.get(pk=v.pk)
            idx = RecordIndex.objects.get(ref=v.ref, project=self.pa)
            self.assertEqual([e.action for e in AuditEntry.objects.filter(record_idx=idx).order_by('pk')],
                             ['created', 'submit'])
        self.assertEqual((v.status, v.visibility, v.ball_in_court_user), ('SUBMITTED', 'SHARED', self.carla))
        self.assertTrue(v.submitted_at)
        self.assertTrue(idx.is_open)
        self.assertEqual([m.to for m in mail.outbox], [['carla@c.test']])

    def test_ball_goes_to_the_contract_administrator_when_a_client(self):
        with org_context(self.org_a):
            from projects.models import ProjectMember
            other = ProjectMember.objects.create(project=self.pa, user=self.nora, role=ProjectMember.ROLE_CLIENT)
            MainContract.objects.create(project=self.pa, form='JCT_IC', administrator=other)
        v = self.submitted()
        self.assertEqual(v.ball_in_court_user, self.nora)

    def test_needs_me_for_the_client(self):
        v = self.submitted()
        with org_context(self.org_a):
            self.assertEqual([i.ref for i in needs_me(self.request_for(self.carla))], [v.ref])
            self.assertEqual(list(needs_me(self.request_for(self.pat))), [])

    def test_only_the_client_approves(self):
        # (who, http status for an approve with notes). Admins may record a decision the client gave.
        cases = [('pat', 403), ('eve', 404), ('connie', 403), ('sid', 403), ('bea', 403), ('nora', 404), ('bob', 404)]
        for name, code in cases:
            v = self.submitted()
            with self.subTest(name):
                self.assertEqual(self.status_for(getattr(self, name), self.detail(v), 'post', {
                    'action': 'decide', 'decision': 'APPROVED', 'decision_notes': 'by phone'}), code)
                with org_context(self.org_a):
                    self.assertEqual(Variation.objects.get(pk=v.pk).status, 'SUBMITTED')

    def test_client_approves_with_po_and_eot(self):
        v = self.submitted(eot_days_requested=5)
        self.login(self.carla)
        r = self.client.post(self.detail(v), {'action': 'decide', 'decision': 'APPROVED', 'approved_value': '900',
                                               'client_reference': 'PO-77', 'eot_days_granted': '3'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            v = Variation.objects.get(pk=v.pk)
        self.assertEqual((v.status, v.approved_value, v.client_reference, v.decided_by, v.eot_days_granted,
                          v.ball_in_court_user), ('APPROVED', Decimal('900.00'), 'PO-77', self.carla, 3, None))

    def test_client_rejects(self):
        v = self.submitted()
        self.login(self.carla)
        self.client.post(self.detail(v), {'action': 'decide', 'decision': 'REJECTED'})
        with org_context(self.org_a):
            v = Variation.objects.get(pk=v.pk)
        self.assertEqual((v.status, v.approved_value), ('REJECTED', None))

    def evidence(self, name='client-email.pdf', body=b'%PDF-1.4 approved by email'):
        return SimpleUploadedFile(name, body, content_type='application/pdf')

    def test_admin_records_client_decision_only_with_notes_and_evidence(self):
        v = self.submitted()
        self.login(self.adam)
        for data in ({'decision': 'APPROVED'},                                           # neither
                     {'decision': 'APPROVED', 'decision_notes': 'Email from C. Client'},    # no evidence
                     {'decision': 'APPROVED', 'evidence': self.evidence()},                 # no note
                     {'decision': 'APPROVED', 'decision_notes': 'x', 'evidence': self.evidence('run.exe')}):  # blocked
            r = self.client.post(self.detail(v), {'action': 'decide', **data})
            self.assertEqual(r.status_code, 200, data)
            with org_context(self.org_a):
                self.assertEqual(Variation.objects.get(pk=v.pk).status, 'SUBMITTED')
        self.assertContains(r, '.exe files are not allowed')
        r = self.client.post(self.detail(v), {'action': 'decide', 'decision': 'APPROVED',
                                              'decision_notes': 'Email from C. Client, 3 Nov', 'evidence': self.evidence()})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            v = Variation.objects.get(pk=v.pk)
            idx = RecordIndex.objects.get(ref=v.ref, project=self.pa)
            entry = AuditEntry.objects.filter(record_idx=idx).order_by('pk').last()
            self.assertTrue(Notification.objects.filter(user=self.carla, title__contains='recorded your approval').exists())
        self.assertEqual((v.status, v.decided_by, v.decision_recorded_on_behalf), ('APPROVED', self.adam, True))
        self.assertTrue(v.decision_evidence.name.endswith('.pdf'))
        self.assertEqual(entry.action, 'approve_on_behalf')
        self.assertIn('Recorded on behalf of client', entry.note)
        # The client can see it was recorded for them, and open the evidence
        self.client.logout()
        self.login(self.carla)
        page = self.client.get(self.detail(v))
        self.assertContains(page, 'Recorded on behalf of the client')
        self.assertEqual(self.client.get(self.url('variation_evidence', self.pa.pk, v.pk)).status_code, 200)
        self.assertEqual(self.status_for(self.sid, self.url('variation_evidence', self.pa.pk, v.pk)), 403)

    def test_admin_records_a_rejection(self):
        v = self.submitted()
        self.login(self.alice)
        self.client.post(self.detail(v), {'action': 'decide', 'decision': 'REJECTED', 'decision_notes': 'Letter 4 Nov',
                                          'evidence': self.evidence('letter.eml', b'From: client')})
        with org_context(self.org_a):
            v = Variation.objects.get(pk=v.pk)
        self.assertEqual((v.status, v.decision_recorded_on_behalf), ('REJECTED', True))

    def test_client_decision_needs_no_evidence_and_is_not_on_behalf(self):
        v = self.submitted()
        self.login(self.carla)
        self.client.post(self.detail(v), {'action': 'decide', 'decision': 'APPROVED'})
        with org_context(self.org_a):
            v = Variation.objects.get(pk=v.pk)
        self.assertEqual((v.status, v.decision_recorded_on_behalf), ('APPROVED', False))

    def test_admins_cannot_use_the_clients_own_transitions(self):
        v = self.submitted()
        with org_context(self.org_a):
            with self.assertRaises(TransitionForbidden):
                apply_transition(v, 'approve', self.adam)
            with self.assertRaises(TransitionForbidden):
                apply_transition(v, 'approve_on_behalf', self.carla)

    def test_client_card_does_not_state_a_contractual_effect(self):
        v = self.submitted()
        self.login(self.carla)
        r = self.client.get(self.detail(v))
        self.assertNotContains(r, 'instructs the contractor')
        self.assertContains(r, 'depends on your contract')

    def test_predicates_directly(self):
        v = self.submitted()
        with org_context(self.org_a):
            for user in (self.pat, self.connie, self.sid):
                with self.assertRaises(TransitionForbidden):
                    apply_transition(v, 'approve', user)
            with self.assertRaises(TransitionForbidden):
                apply_transition(v, 'withdraw', self.carla)
            apply_transition(v, 'approve', self.carla)
        self.assertEqual(v.status, 'APPROVED')

    def test_client_cannot_submit_or_withdraw(self):
        v = self.vo()
        with org_context(self.org_a):
            apply_transition(v, 'submit', self.pat)
        for action in ('withdraw',):
            self.assertEqual(self.status_for(self.carla, self.detail(v), 'post', {'action': action}), 403)

    def test_decided_cannot_be_edited(self):
        v = self.submitted()
        with org_context(self.org_a):
            apply_transition(v, 'approve', self.carla)
        self.login(self.pat)
        r = self.client.get(self.url('variation_edit', self.pa.pk, v.pk))
        self.assertEqual(r.status_code, 302)


class FormAndLinkTests(VariationTestBase):
    def test_create_links_the_rfi_and_edit_moves_the_link(self):
        with org_context(self.org_a):
            r1 = RFI(project=self.pa, title='Veneer?', question='?')
            r1.save(actor=self.pat)
            r2 = RFI(project=self.pa, title='Ironmongery?', question='?')
            r2.save(actor=self.pat)
        self.login(self.pat)
        data = {'title': 'Veneer change', 'cause': 'SPEC_CHANGE', 'description': 'x', 'value': '700',
                'rfi': r1.pk, 'eot_days_requested': '2', 'quotation_due': '2026-11-20'}
        r = self.client.post(self.url('variation_create', self.pa.pk), data)
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            v = Variation.objects.get(title='Veneer change')
            self.assertEqual([i.ref for i in came_from(v, 'rfis.rfi')], ['RFI-001'])
            self.assertEqual((v.eot_days_requested, str(v.quotation_due)), (2, '2026-11-20'))
        r = self.client.post(self.url('variation_edit', self.pa.pk, v.pk), {**data, 'rfi': r2.pk, 'version': v.version})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.assertEqual([i.ref for i in came_from(v, 'rfis.rfi')], ['RFI-002'])
            self.assertEqual(Variation.objects.get(pk=v.pk).rfi_id, r2.pk)  # the FK stays too

    def test_stale_form_is_refused(self):
        v = self.vo()
        self.login(self.pat)
        r = self.client.post(self.url('variation_edit', self.pa.pk, v.pk), {
            'title': 'Changed', 'cause': 'OTHER', 'description': 'x', 'version': v.version - 1 or 99})
        self.assertContains(r, 'Someone else changed this')

    def test_csv_keeps_its_columns(self):
        self.submitted()
        self.login(self.pat)
        r = self.client.get(self.url('variation_list', self.pa.pk) + '?export=csv')
        header = r.content.decode().splitlines()[0]
        self.assertTrue(header.startswith('Ref,Title,Cause,Status,Quoted,Approved,Time impact (days),At risk,'
                                          'Client ref,Submitted,Decided,Decided by'))

    def test_list_filter_and_empty_state(self):
        self.vo()
        self.login(self.pat)
        r = self.client.get(self.url('variation_list', self.pa.pk) + '?status=APPROVED')
        self.assertContains(r, 'Nothing with this status')


class VisibilityTests(VariationTestBase):
    def test_who_sees_the_list(self):
        url = self.url('variation_list', self.pa.pk)
        for name, code in [('alice', 200), ('pat', 200), ('carla', 200), ('connie', 200), ('sid', 403), ('bea', 403),
                           ('nora', 404), ('eve', 404), ('bob', 404)]:
            with self.subTest(name):
                self.assertEqual(self.status_for(getattr(self, name), url), code)

    def test_client_index_has_shared_variations_only(self):
        draft, shared = self.vo(title='Draft one'), self.submitted(title='Shared one')
        with org_context(self.org_a):
            self.assertEqual({i.ref for i in visible_index(self.request_for(self.carla))}, {shared.ref})
            self.assertEqual({i.ref for i in visible_index(self.request_for(self.pat))}, {draft.ref, shared.ref})
        self.assertNotEqual(draft.ref, shared.ref)

    def test_other_company_and_no_context(self):
        v = self.vo()
        self.login(self.bob)
        self.assertEqual(self.client.get(self.detail(v)).status_code, 404)
        with org_context(self.org_b):
            self.assertFalse(Variation.objects.filter(pk=v.pk).exists())
        with self.assertRaises(TenantContextMissing):
            Variation.objects.count()


class RespondByTests(VariationTestBase):
    def test_submit_sets_respond_by_from_the_contract(self):
        today = timezone.localdate()
        v = self.submitted()
        self.assertEqual(v.due_date, today + timedelta(days=14))  # no contract: the default
        with org_context(self.org_a):
            MainContract.objects.create(project=self.pa, form='JCT_IC', client_response_days=10)
            self.assertEqual(RecordIndex.objects.get(ref=v.ref, project=self.pa).due_date, today + timedelta(days=14))
        v2 = self.submitted()
        self.assertEqual(v2.due_date, today + timedelta(days=10))

    def test_staff_can_change_it_and_overdue_shows_red(self):
        v = self.submitted(title='Late decision')
        self.login(self.pat)
        past = timezone.localdate() - timedelta(days=2)
        r = self.client.post(self.url('variation_edit', self.pa.pk, v.pk), {
            'title': 'Late decision', 'cause': 'OTHER', 'description': 'x', 'value': '1000', 'due_date': past.isoformat(),
            'version': v.version})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            v = Variation.objects.get(pk=v.pk)
            self.assertEqual(RecordIndex.objects.get(ref=v.ref, project=self.pa).due_date, past)
        self.assertTrue(v.is_overdue)
        self.assertContains(self.client.get(self.url('variation_list', self.pa.pk)), 'overdue">')
        self.assertContains(self.client.get(self.detail(v)), '(overdue)')

    def test_drafts_have_no_respond_by_field(self):
        v = self.vo()
        self.login(self.pat)
        self.assertNotContains(self.client.get(self.url('variation_edit', self.pa.pk, v.pk)), 'Client to respond by')

    def test_decided_variations_are_not_overdue(self):
        v = self.submitted()
        with org_context(self.org_a):
            v.due_date = timezone.localdate() - timedelta(days=5)
            v.save(actor=self.pat)
            apply_transition(v, 'approve', self.carla)
        self.assertFalse(v.is_overdue)


class ExternalIndexTests(VariationTestBase):
    def test_subcontractors_and_bidders_never_get_variation_index_rows(self):
        # Regression: shared VO rows were returned to subcontractors by core.access.visible_index
        v = self.submitted()
        with org_context(self.org_a):
            from rfis.models import RFI
            rfi = RFI(project=self.pa, title='Shared question', question='?')
            rfi.save(actor=self.pat)
            for who in (self.sid, self.bea):
                refs = set(visible_index(self.request_for(who)).values_list('ref', flat=True))
                self.assertNotIn(v.ref, refs, who)
            self.assertIn(rfi.ref, set(visible_index(self.request_for(self.sid)).values_list('ref', flat=True)))
            for who in (self.carla, self.connie):
                self.assertIn(v.ref, set(visible_index(self.request_for(who)).values_list('ref', flat=True)), who)
            v.ball_in_court_user = self.sid
            v.save(actor=self.pat)
            self.assertEqual(list(needs_me(self.request_for(self.sid))), [])

    def test_role_is_per_project(self):
        # Sid is a subcontractor on pa but a client on another job: he sees that job's VOs only
        from projects.models import Project, ProjectMember
        with org_context(self.org_a):
            other = Project.objects.create(name='Other job', stage=Project.STAGE_LIVE)
            ProjectMember.objects.create(project=other, user=self.sid, role=ProjectMember.ROLE_CLIENT)
        on_pa = self.submitted()
        on_other = self.vo(project=other)
        with org_context(self.org_a):
            apply_transition(on_other, 'submit', self.pat)
            rows = set(visible_index(self.request_for(self.sid)).values_list('project_id', 'ref'))
        self.assertIn((other.pk, on_other.ref), rows)
        self.assertNotIn((self.pa.pk, on_pa.ref), rows)
