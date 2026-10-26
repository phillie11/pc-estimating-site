"""
The tests that matter most for a multi-tenant product:
one company must never see or touch another company's data.
"""
import json
import time
from datetime import timedelta
from decimal import Decimal

import stripe
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.views import create_organization
from billing.models import StripeEvent
from directory.models import Subcontractor
from projects.models import Document, Issue, Project, ProjectInvite, ProjectMember
from rfis.models import RFI
from variations.models import Variation

from .context import org_context
from .models import Membership, Organization, TeamInvite

PW = 'Correct-Horse-9'


def make_user(email, name='Test User'):
    return User.objects.create_user(email=email, password=PW, full_name=name)


class Base(TestCase):
    def setUp(self):
        self.alice = make_user('alice@a.test', 'Alice A')
        self.bob = make_user('bob@b.test', 'Bob B')
        self.org_a = create_organization('Alpha Fitout', self.alice)
        self.org_b = create_organization('Bravo Build', self.bob)
        with org_context(self.org_a):
            self.pa = Project.objects.create(name='Alpha secret job', stage=Project.STAGE_LIVE, value=Decimal('100000'))
            self.rfi_a = RFI.objects.create(project=self.pa, subject='A question', question='?', raised_by=self.alice)
            self.vo_a = Variation.objects.create(project=self.pa, title='A VO', description='x', value=Decimal('500'))
            self.sub_a = Subcontractor.objects.create(company_name='Alpha Sparks')
        with org_context(self.org_b):
            self.pb = Project.objects.create(name='Bravo job', stage=Project.STAGE_LIVE)

    def login(self, user):
        self.client.force_login(user, backend='django.contrib.auth.backends.ModelBackend')

    def url(self, name, org, *args):
        return reverse(name, args=[org.slug, *args])


class TenantIsolationTests(Base):
    def test_cannot_open_another_orgs_workspace(self):
        self.login(self.bob)
        self.assertEqual(self.client.get(self.url('dashboard', self.org_a)).status_code, 404)

    def test_cannot_reach_other_orgs_objects_through_own_workspace(self):
        """Swap the object id under your own slug: TenantManager must hide it."""
        self.login(self.bob)
        for name, args in [('project_detail', [self.pa.pk]), ('rfi_detail', [self.pa.pk, self.rfi_a.pk]),
                           ('variation_detail', [self.pa.pk, self.vo_a.pk]), ('sub_detail', [self.sub_a.pk])]:
            r = self.client.get(self.url(name, self.org_b, *args))
            self.assertEqual(r.status_code, 404, name)

    def test_lists_only_show_own_rows(self):
        self.login(self.bob)
        r = self.client.get(self.url('project_list', self.org_b) + '?view=all')
        self.assertContains(r, 'Bravo job')
        self.assertNotContains(r, 'Alpha secret job')
        r = self.client.get(self.url('sub_list', self.org_b))
        self.assertNotContains(r, 'Alpha Sparks')

    def test_manager_scopes_queries_to_current_org(self):
        with org_context(self.org_b):
            self.assertEqual(list(Project.objects.values_list('name', flat=True)), ['Bravo job'])
        self.assertEqual(Project.all_objects.count(), 2)

    def test_cross_org_write_is_refused(self):
        with org_context(self.org_b):
            p = Project.all_objects.get(pk=self.pa.pk)
            p.name = 'hijacked'
            with self.assertRaises(PermissionError):
                p.save()

    def test_form_choices_do_not_leak_other_org_users(self):
        """Assignee dropdowns list only this org's people."""
        self.login(self.alice)
        r = self.client.get(self.url('rfi_create', self.org_a, self.pa.pk))
        self.assertContains(r, 'Alice A')
        self.assertNotContains(r, 'Bob B')

    def test_post_with_other_orgs_user_id_is_rejected(self):
        self.login(self.alice)
        r = self.client.post(self.url('rfi_create', self.org_a, self.pa.pk), {
            'subject': 'x', 'question': 'y', 'category': 'DESIGN', 'priority': 'NORMAL',
            'assigned_to': self.bob.pk, 'cost_impact': 'TBC', 'is_shared': 'on'})
        self.assertEqual(r.status_code, 200)  # form re-rendered with error
        with org_context(self.org_a):
            self.assertFalse(RFI.objects.filter(subject='x').exists())

    def test_numbering_is_per_project(self):
        with org_context(self.org_a):
            r2 = RFI.objects.create(project=self.pa, subject='Second', question='?')
        self.assertEqual(self.rfi_a.reference, 'RFI-001')
        self.assertEqual(r2.reference, 'RFI-002')
        self.assertEqual(self.pa.reference, 'P-0001')
        self.assertEqual(self.pb.reference, 'P-0001')  # each org starts its own sequence


class ExternalPortalTests(Base):
    def setUp(self):
        super().setUp()
        self.client_user = make_user('client@c.test', 'Carla Client')
        self.sub_user = make_user('sub@s.test', 'Sid Sub')
        with org_context(self.org_a):
            Membership.objects.create(organization=self.org_a, user=self.client_user, role=Membership.ROLE_EXTERNAL)
            Membership.objects.create(organization=self.org_a, user=self.sub_user, role=Membership.ROLE_EXTERNAL)
            ProjectMember.objects.create(project=self.pa, user=self.client_user, role=ProjectMember.ROLE_CLIENT)
            ProjectMember.objects.create(project=self.pa, user=self.sub_user, role=ProjectMember.ROLE_SUBCONTRACTOR)
            self.other = Project.objects.create(name='Unshared job', stage=Project.STAGE_LIVE)
            self.internal_rfi = RFI.objects.create(project=self.pa, subject='Internal only', question='?', is_shared=False)
            self.vo_a.status = Variation.STATUS_SUBMITTED
            self.vo_a.save()
            self.draft = Variation.objects.create(project=self.pa, title='Draft VO', description='x', value=Decimal('1'))
            self.doc_internal = Document.objects.create(project=self.pa, title='Cost plan', visibility=Document.VIS_INTERNAL)
            self.doc_shared = Document.objects.create(project=self.pa, title='GA drawing', visibility=Document.VIS_SHARED)

    def test_external_sees_only_their_projects(self):
        self.login(self.client_user)
        r = self.client.get(self.url('project_list', self.org_a) + '?view=all')
        self.assertContains(r, 'Alpha secret job')
        self.assertNotContains(r, 'Unshared job')
        self.assertEqual(self.client.get(self.url('project_detail', self.org_a, self.other.pk)).status_code, 404)

    def test_external_cannot_use_staff_areas(self):
        self.login(self.sub_user)
        for name in ('pipeline', 'sub_list', 'team', 'project_create'):
            self.assertEqual(self.client.get(self.url(name, self.org_a)).status_code, 403, name)
        self.assertEqual(self.client.get(self.url('package_list', self.org_a, self.pa.pk)).status_code, 403)

    def test_external_does_not_see_commercials(self):
        self.login(self.client_user)
        r = self.client.get(self.url('project_detail', self.org_a, self.pa.pk))
        self.assertNotContains(r, 'Forecast final account')
        self.assertNotContains(r, '£100,000')

    def test_internal_rfis_and_documents_hidden(self):
        self.login(self.sub_user)
        self.assertEqual(self.client.get(self.url('rfi_detail', self.org_a, self.pa.pk, self.internal_rfi.pk)).status_code, 404)
        r = self.client.get(self.url('document_list', self.org_a, self.pa.pk))
        self.assertContains(r, 'GA drawing')
        self.assertNotContains(r, 'Cost plan')

    def test_subcontractor_cannot_see_variations(self):
        self.login(self.sub_user)
        self.assertEqual(self.client.get(self.url('variation_list', self.org_a, self.pa.pk)).status_code, 403)

    def test_client_sees_submitted_not_draft_and_can_approve(self):
        self.login(self.client_user)
        r = self.client.get(self.url('variation_list', self.org_a, self.pa.pk))
        self.assertContains(r, 'A VO')
        self.assertNotContains(r, 'Draft VO')
        r = self.client.post(self.url('variation_detail', self.org_a, self.pa.pk, self.vo_a.pk),
                             {'action': 'decide', 'decision': 'APPROVED', 'approved_value': '450', 'client_reference': 'PO-1'})
        self.assertEqual(r.status_code, 302)
        self.vo_a.refresh_from_db()
        self.assertEqual(self.vo_a.status, Variation.STATUS_APPROVED)
        self.assertEqual(self.vo_a.approved_value, Decimal('450'))
        self.assertEqual(self.vo_a.decided_by, self.client_user)

    def test_assignee_can_respond_to_rfi_but_not_close(self):
        with org_context(self.org_a):
            self.rfi_a.assigned_to = self.client_user
            self.rfi_a.save()
        self.login(self.client_user)
        r = self.client.post(self.url('rfi_detail', self.org_a, self.pa.pk, self.rfi_a.pk),
                             {'action': 'respond', 'response': 'Use oak.', 'close': 'on'})
        self.assertEqual(r.status_code, 302)
        self.rfi_a.refresh_from_db()
        self.assertEqual(self.rfi_a.status, RFI.STATUS_RESPONDED)
        self.assertEqual(self.rfi_a.response, 'Use oak.')

    def test_non_assignee_external_cannot_respond(self):
        self.login(self.sub_user)
        r = self.client.post(self.url('rfi_detail', self.org_a, self.pa.pk, self.rfi_a.pk),
                             {'action': 'respond', 'response': 'sneaky'})
        self.assertEqual(r.status_code, 403)

    def test_internal_comments_hidden_from_externals(self):
        self.login(self.alice)
        self.client.post(self.url('rfi_detail', self.org_a, self.pa.pk, self.rfi_a.pk),
                         {'action': 'comment', 'body': 'Staff-only remark', 'internal_only': 'on'})
        self.client.post(self.url('rfi_detail', self.org_a, self.pa.pk, self.rfi_a.pk),
                         {'action': 'comment', 'body': 'Public remark'})
        self.login(self.client_user)
        r = self.client.get(self.url('rfi_detail', self.org_a, self.pa.pk, self.rfi_a.pk))
        self.assertContains(r, 'Public remark')
        self.assertNotContains(r, 'Staff-only remark')

    def test_external_upload_is_forced_shared(self):
        self.login(self.sub_user)
        f = SimpleUploadedFile('rams.pdf', b'%PDF-1.4 test', content_type='application/pdf')
        r = self.client.post(self.url('document_list', self.org_a, self.pa.pk), {
            'title': 'Joinery RAMS', 'reference': 'R1', 'category': 'RAMS', 'visibility': 'INTERNAL',
            'file': f, 'revision': 'P01'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.assertEqual(Document.objects.get(title='Joinery RAMS').visibility, Document.VIS_SHARED)

    def test_blocked_file_types_rejected(self):
        self.login(self.alice)
        f = SimpleUploadedFile('evil.html', b'<script>alert(1)</script>', content_type='text/html')
        r = self.client.post(self.url('document_list', self.org_a, self.pa.pk), {
            'title': 'x', 'category': 'OTHER', 'visibility': 'SHARED', 'file': f, 'revision': 'P01'})
        self.assertContains(r, 'not allowed')

    def test_external_cannot_download_internal_document(self):
        with org_context(self.org_a):
            from projects.models import DocumentRevision
            rev = DocumentRevision.objects.create(
                document=self.doc_internal, file=SimpleUploadedFile('cp.pdf', b'%PDF secret'), original_name='cp.pdf')
        self.login(self.client_user)
        r = self.client.get(self.url('document_download', self.org_a, self.pa.pk, self.doc_internal.pk, rev.pk))
        self.assertEqual(r.status_code, 404)

    def test_external_only_sees_own_snags(self):
        with org_context(self.org_a):
            mine = Issue.objects.create(project=self.pa, title='Sub snag', assignee=self.sub_user)
            other = Issue.objects.create(project=self.pa, title='Someone else snag', assignee=self.alice)
        self.login(self.sub_user)
        r = self.client.get(self.url('issue_list', self.org_a, self.pa.pk))
        self.assertContains(r, 'Sub snag')
        self.assertNotContains(r, 'Someone else snag')
        self.assertEqual(self.client.get(self.url('issue_detail', self.org_a, self.pa.pk, other.pk)).status_code, 403)
        self.client.post(self.url('issue_detail', self.org_a, self.pa.pk, mine.pk), {'action': 'ready'})
        mine.refresh_from_db()
        self.assertEqual(mine.status, Issue.STATUS_READY)


class InviteTests(Base):
    def test_project_invite_new_user_flow(self):
        self.login(self.alice)
        self.client.post(self.url('project_team', self.org_a, self.pa.pk), {
            'action': 'invite', 'email': 'NewClient@x.test', 'role': 'CLIENT', 'company_name': 'X Ltd'})
        inv = ProjectInvite.all_objects.get(email='newclient@x.test')
        self.client.logout()
        r = self.client.post(reverse('accept_project_invite', args=[inv.token]), {'full_name': 'New Client', 'password': PW})
        self.assertEqual(r.status_code, 302)
        u = User.objects.get(email='newclient@x.test')
        self.assertTrue(Membership.objects.filter(organization=self.org_a, user=u, role=Membership.ROLE_EXTERNAL).exists())
        self.assertTrue(ProjectMember.all_objects.filter(project=self.pa, user=u, role='CLIENT').exists())
        inv.refresh_from_db()
        self.assertEqual(inv.status, ProjectInvite.STATUS_ACCEPTED)
        # The token is single use
        self.client.logout()
        self.assertEqual(self.client.get(reverse('accept_project_invite', args=[inv.token])).status_code, 410)

    def test_invite_for_someone_else_is_refused(self):
        with org_context(self.org_a):
            inv = ProjectInvite.objects.create(project=self.pa, email='someone@x.test', role='CLIENT')
        self.login(self.bob)
        self.assertEqual(self.client.post(reverse('accept_project_invite', args=[inv.token])).status_code, 403)

    def test_team_invite_respects_seat_limit(self):
        self.org_a.plan = 'starter'  # 3 seats
        self.org_a.save()
        self.login(self.alice)
        for i in range(2):
            self.client.post(self.url('team', self.org_a), {'email': f'p{i}@a.test', 'role': 'MEMBER'})
        self.assertEqual(TeamInvite.objects.filter(organization=self.org_a).count(), 2)
        self.client.post(self.url('team', self.org_a), {'email': 'p9@a.test', 'role': 'MEMBER'})
        self.assertEqual(TeamInvite.objects.filter(organization=self.org_a).count(), 2)

    def test_last_owner_cannot_be_removed(self):
        self.login(self.alice)
        m = Membership.objects.get(organization=self.org_a, user=self.alice)
        self.client.post(self.url('member_update', self.org_a, m.pk), {'action': 'role', 'role': 'MEMBER'})
        m.refresh_from_db()
        self.assertEqual(m.role, Membership.ROLE_OWNER)

    def test_staff_cannot_promote_to_owner(self):
        carol = make_user('carol@a.test')
        admin = Membership.objects.create(organization=self.org_a, user=carol, role=Membership.ROLE_ADMIN)
        dave = make_user('dave@a.test')
        m = Membership.objects.create(organization=self.org_a, user=dave, role=Membership.ROLE_MEMBER)
        self.login(carol)
        self.client.post(self.url('member_update', self.org_a, m.pk), {'action': 'role', 'role': 'OWNER'})
        m.refresh_from_db()
        self.assertEqual(m.role, Membership.ROLE_MEMBER)


class SignupTests(TestCase):
    def test_signup_creates_org_with_trial(self):
        r = self.client.post(reverse('signup'), {'company_name': 'Admin Ltd', 'full_name': 'Pat', 'email': 'pat@p.test',
                                                 'password': PW})
        org = Organization.objects.get(name='Admin Ltd')
        # The dashboard sends staff on to their section's home, so only check the first hop
        self.assertRedirects(r, reverse('dashboard', args=[org.slug]), fetch_redirect_response=False)
        self.assertNotEqual(org.slug, 'admin')  # reserved word avoided
        self.assertTrue(org.has_access)
        self.assertEqual(org.subscription_status, Organization.STATUS_TRIAL)
        from directory.models import Trade
        self.assertTrue(Trade.all_objects.filter(organization=org).exists())
        self.assertTrue(Membership.objects.filter(organization=org, role=Membership.ROLE_OWNER).exists())


@override_settings(STRIPE_WEBHOOK_SECRET='whsec_test', STRIPE_SECRET_KEY='', PLANS={
    'starter': {'name': 'Starter', 'price_label': '£1', 'interval': 'month', 'stripe_price_id': 'price_S',
                'max_staff': 3, 'max_active_projects': 1, 'blurb': ''},
    'team': {'name': 'Team', 'price_label': '£2', 'interval': 'month', 'stripe_price_id': 'price_T',
             'max_staff': 10, 'max_active_projects': 50, 'blurb': ''},
})
class BillingTests(Base):
    def post_event(self, event):
        payload = json.dumps(event)
        ts = int(time.time())
        sig = stripe.WebhookSignature._compute_signature(f'{ts}.{payload}', 'whsec_test')
        return self.client.post(reverse('stripe_webhook'), payload, content_type='application/json',
                                HTTP_STRIPE_SIGNATURE=f't={ts},v1={sig}')

    def sub_event(self, eid, status, price='price_T', etype='customer.subscription.updated'):
        return {'id': eid, 'type': etype, 'data': {'object': {
            'id': 'sub_1', 'customer': 'cus_1', 'status': status, 'metadata': {'org_id': str(self.org_a.pk)},
            'cancel_at_period_end': False, 'current_period_end': int(time.time()) + 86400 * 30,
            'items': {'data': [{'price': {'id': price}}]}}}}

    def test_trial_expiry_locks_workspace(self):
        self.org_a.trial_ends_at = timezone.now() - timedelta(days=1)
        self.org_a.save()
        self.login(self.alice)
        r = self.client.get(self.url('dashboard', self.org_a))
        self.assertRedirects(r, self.url('billing', self.org_a))
        self.assertEqual(self.client.get(self.url('billing', self.org_a)).status_code, 200)

    def test_locked_workspace_blocks_externals_too(self):
        ext = make_user('e@e.test')
        Membership.objects.create(organization=self.org_a, user=ext, role=Membership.ROLE_EXTERNAL)
        self.org_a.trial_ends_at = timezone.now() - timedelta(days=1)
        self.org_a.save()
        self.login(ext)
        self.assertEqual(self.client.get(self.url('dashboard', self.org_a)).status_code, 402)

    def test_bad_signature_rejected(self):
        r = self.client.post(reverse('stripe_webhook'), '{}', content_type='application/json',
                             HTTP_STRIPE_SIGNATURE='t=1,v1=bad')
        self.assertEqual(r.status_code, 400)

    def test_subscription_activates_and_sets_plan(self):
        r = self.post_event(self.sub_event('evt_1', 'active', 'price_S'))
        self.assertEqual(r.status_code, 200)
        self.org_a.refresh_from_db()
        self.assertEqual(self.org_a.subscription_status, 'active')
        self.assertEqual(self.org_a.plan, 'starter')
        self.assertEqual(self.org_a.stripe_customer_id, 'cus_1')
        self.assertIsNotNone(self.org_a.current_period_end)

    def test_webhook_is_idempotent(self):
        self.post_event(self.sub_event('evt_2', 'active'))
        r = self.post_event(self.sub_event('evt_2', 'canceled'))  # same id replayed with different body
        self.assertEqual(r.content, b'duplicate')
        self.org_a.refresh_from_db()
        self.assertEqual(self.org_a.subscription_status, 'active')
        self.assertEqual(StripeEvent.objects.count(), 1)

    def test_past_due_has_grace_then_locks(self):
        self.post_event(self.sub_event('evt_3', 'active'))
        self.post_event(self.sub_event('evt_4', 'past_due'))
        self.org_a.refresh_from_db()
        self.assertTrue(self.org_a.has_access)
        self.org_a.payment_failed_at = timezone.now() - timedelta(days=30)
        self.org_a.save()
        self.assertFalse(self.org_a.has_access)

    def test_cancellation_locks(self):
        self.post_event(self.sub_event('evt_5', 'active'))
        self.post_event(self.sub_event('evt_6', 'canceled', etype='customer.subscription.deleted'))
        self.org_a.refresh_from_db()
        self.assertEqual(self.org_a.subscription_status, 'canceled')
        self.assertFalse(self.org_a.has_access)

    def test_project_limit_enforced(self):
        self.org_a.plan = 'starter'  # max 1 active project, org_a already has one
        self.org_a.save()
        self.login(self.alice)
        self.client.post(self.url('project_create', self.org_a), {'name': 'Over limit', 'stage': 'LEAD'})
        with org_context(self.org_a):
            self.assertFalse(Project.objects.filter(name='Over limit').exists())

    def test_only_admins_reach_billing(self):
        staff = make_user('s@a.test')
        Membership.objects.create(organization=self.org_a, user=staff, role=Membership.ROLE_MEMBER)
        self.login(staff)
        self.assertEqual(self.client.post(self.url('billing_checkout', self.org_a), {'plan': 'team'}).status_code, 403)


class SecurityReviewRegressionTests(Base):
    """One test per finding from the independent access-control review."""

    def test_removed_staff_return_only_as_external(self):
        pm = make_user('pm@a.test')
        m = Membership.objects.create(organization=self.org_a, user=pm, role=Membership.ROLE_ADMIN, is_active=False)
        with org_context(self.org_a):
            other = Project.objects.create(name='Other', stage=Project.STAGE_LIVE)
            inv = ProjectInvite.objects.create(project=other, email='pm@a.test', role='SUBCONTRACTOR')
        self.login(pm)
        self.client.post(reverse('accept_project_invite', args=[inv.token]))
        m.refresh_from_db()
        self.assertEqual(m.role, Membership.ROLE_EXTERNAL)
        self.assertEqual(self.client.get(self.url('sub_list', self.org_a)).status_code, 403)

    def test_external_feed_hides_internal_and_variation_events(self):
        sub = make_user('s@s.test')
        Membership.objects.create(organization=self.org_a, user=sub, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=sub, role='SUBCONTRACTOR')
            hidden = RFI.objects.create(project=self.pa, subject='Hidden', question='?', is_shared=False)
        self.login(self.alice)
        self.client.post(self.url('rfi_detail', self.org_a, self.pa.pk, hidden.pk), {'action': 'comment', 'body': 'x'})
        self.client.post(self.url('variation_detail', self.org_a, self.pa.pk, self.vo_a.pk), {'action': 'submit'})
        self.client.logout()
        self.login(sub)
        self.client.get(self.url('activity', self.org_a))  # consume any queued flash messages
        r = self.client.get(self.url('activity', self.org_a))
        self.assertNotContains(r, hidden.reference)
        self.assertNotContains(r, self.vo_a.reference)

    def test_staff_member_cannot_approve_variation(self):
        staff = make_user('st@a.test')
        Membership.objects.create(organization=self.org_a, user=staff, role=Membership.ROLE_MEMBER)
        with org_context(self.org_a):
            self.vo_a.status = Variation.STATUS_SUBMITTED
            self.vo_a.save()
        self.login(staff)
        r = self.client.post(self.url('variation_detail', self.org_a, self.pa.pk, self.vo_a.pk),
                             {'action': 'decide', 'decision': 'APPROVED', 'approved_value': '99999'})
        self.assertEqual(r.status_code, 403)

    def test_admin_recording_decision_must_give_notes(self):
        with org_context(self.org_a):
            self.vo_a.status = Variation.STATUS_SUBMITTED
            self.vo_a.save()
        self.login(self.alice)
        self.client.post(self.url('variation_detail', self.org_a, self.pa.pk, self.vo_a.pk),
                         {'action': 'decide', 'decision': 'APPROVED'})
        self.vo_a.refresh_from_db()
        self.assertEqual(self.vo_a.status, Variation.STATUS_SUBMITTED)

    def test_admin_cannot_remove_owner(self):
        admin = make_user('ad@a.test')
        Membership.objects.create(organization=self.org_a, user=admin, role=Membership.ROLE_ADMIN)
        second = make_user('o2@a.test')
        Membership.objects.create(organization=self.org_a, user=second, role=Membership.ROLE_OWNER)
        owner_m = Membership.objects.get(organization=self.org_a, user=self.alice)
        self.login(admin)
        self.client.post(self.url('member_update', self.org_a, owner_m.pk), {'action': 'remove'})
        owner_m.refresh_from_db()
        self.assertTrue(owner_m.is_active)

    def test_external_cannot_revise_contractors_document(self):
        sub = make_user('s2@s.test')
        Membership.objects.create(organization=self.org_a, user=sub, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=sub, role='SUBCONTRACTOR')
            doc = Document.objects.create(project=self.pa, title='GA', visibility=Document.VIS_SHARED, created_by=self.alice)
        self.login(sub)
        f = SimpleUploadedFile('c99.pdf', b'%PDF')
        r = self.client.post(self.url('document_detail', self.org_a, self.pa.pk, doc.pk), {'file': f, 'revision': 'C99'})
        self.assertEqual(r.status_code, 403)

    def test_task_update_redirect_stays_on_site(self):
        with org_context(self.org_a):
            from projects.models import Task
            t = Task.objects.create(project=self.pa, title='T')
        self.login(self.alice)
        r = self.client.post(self.url('task_update', self.org_a, self.pa.pk, t.pk),
                             {'status': 'DONE', 'next': 'https://evil.example/phish'})
        self.assertNotIn('evil.example', r['Location'])

    def test_external_to_staff_respects_seat_limit(self):
        self.org_a.plan = 'starter'
        self.org_a.save()
        for i in range(2):
            Membership.objects.create(organization=self.org_a, user=make_user(f'x{i}@a.test'), role=Membership.ROLE_MEMBER)
        ext = Membership.objects.create(organization=self.org_a, user=make_user('ext@e.test'), role=Membership.ROLE_EXTERNAL)
        self.login(self.alice)
        self.client.post(self.url('member_update', self.org_a, ext.pk), {'action': 'role', 'role': 'MEMBER'})
        ext.refresh_from_db()
        self.assertEqual(ext.role, Membership.ROLE_EXTERNAL)


@override_settings(STRIPE_WEBHOOK_SECRET='whsec_test', STRIPE_SECRET_KEY='')
class StaleSubscriptionTests(BillingTests):
    def test_deleting_old_subscription_does_not_lock(self):
        self.post_event(self.sub_event('evt_a', 'active'))  # sub_1 is current
        old = self.sub_event('evt_b', 'canceled', etype='customer.subscription.deleted')
        old['data']['object']['id'] = 'sub_old'
        self.post_event(old)
        self.org_a.refresh_from_db()
        self.assertEqual(self.org_a.subscription_status, 'active')
