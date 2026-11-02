"""
Permits to work through the views: asking for one (defaults, checks on the times), approval by the PM
or an admin confirming every precaution, refusal with a reason, closing (with the fire watch for hot
works), the state from the clock, Needs me for the approver and for expiring and expired permits,
and the printable page.
"""
import datetime

from django.core import mail
from django.test import override_settings
from django.utils import timezone

import actions
from tenancy.context import org_context
from tenancy.models import Membership

from .. import rules
from ..models import Permit, QualitySettings
from .base import MEDIA, QualityTestBase, tearDownModule  # noqa: F401


def local(dt):
    return timezone.localtime(dt).strftime('%Y-%m-%dT%H:%M')


@override_settings(MEDIA_ROOT=MEDIA)
class PermitFlowTests(QualityTestBase):
    def form(self, **kw):
        start = timezone.now() + datetime.timedelta(hours=1)
        data = {'permit_type': rules.HOT_WORKS, 'title': 'Weld brackets', 'location': 'Bar',
                'starts_at': local(start), 'expires_at': local(start + datetime.timedelta(hours=8))}
        data.update(kw)
        return data

    def test_empty_list_says_what_goes_here(self):
        self.login(self.pat)
        r = self.client.get(self.purl('permit_list'))
        self.assertContains(r, 'No permits to work yet')
        self.assertContains(r, 'Ask for a permit')

    def test_ask_for_one_defaults_and_goes_to_the_pm(self):
        with org_context(self.org_a):
            QualitySettings.objects.filter(organization=self.org_a).update(permit_hours=6)
        self.login(self.sam)
        r = self.client.get(self.purl('permit_create'))
        start, end = r.context['form'].initial['starts_at'], r.context['form'].initial['expires_at']
        self.assertEqual(end - start, datetime.timedelta(hours=6))
        self.assertEqual(start.minute % 15, 0)
        r = self.client.post(self.purl('permit_create'), self.form(holder='Site welder', package=self.pkg.pk))
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            p = Permit.objects.get()
        self.assertEqual((p.ref, p.status, p.ball_in_court_user, p.created_by), ('PTW-001', rules.REQUESTED, self.pat,
                                                                                 self.sam))
        self.assertEqual(p.due_date, timezone.localdate(p.starts_at))
        self.assertTrue(any('permit to approve' in m.subject for m in mail.outbox))
        self.assertEqual(p.state, 'requested')

    def test_required_fields_say_how_to_fix(self):
        self.login(self.sam)
        r = self.client.post(self.purl('permit_create'), {'starts_at': '', 'expires_at': ''})
        self.assertContains(r, 'Choose the type of permit.')
        self.assertContains(r, 'Say what work the permit is for.')
        self.assertContains(r, 'Say where the work is.')

    def test_times_are_checked(self):
        self.login(self.sam)
        start = timezone.now() + datetime.timedelta(hours=1)
        r = self.client.post(self.purl('permit_create'), self.form(expires_at=local(start - datetime.timedelta(hours=1)),
                                                                   starts_at=local(start)))
        self.assertContains(r, 'must run out after it starts')
        r = self.client.post(self.purl('permit_create'), self.form(starts_at=local(start),
                                                                   expires_at=local(start + datetime.timedelta(hours=20))))
        self.assertContains(r, '12 hours at most')
        past = timezone.now() - datetime.timedelta(hours=5)
        r = self.client.post(self.purl('permit_create'), self.form(starts_at=local(past),
                                                                   expires_at=local(past + datetime.timedelta(hours=2))))
        self.assertContains(r, 'already passed')
        with org_context(self.org_a):
            self.assertFalse(Permit.objects.exists())

    def test_approval_needs_every_precaution_and_the_pm_or_an_admin(self):
        p = self.permit()
        all_ticked = {'action': 'approve', 'precautions': rules.precautions_for(rules.HOT_WORKS)}
        self.login(self.sam)  # site manager: can ask, can't approve
        self.assertEqual(self.client.post(self.purl('permit_action', p.pk), all_ticked).status_code, 403)
        self.assertNotContains(self.client.get(p.get_absolute_url()), 'Approve permit')
        self.login(self.pat)
        some = {'action': 'approve', 'precautions': rules.precautions_for(rules.HOT_WORKS)[:2]}
        r = self.client.post(self.purl('permit_action', p.pk), some, follow=True)
        self.assertContains(r, 'Tick every precaution')
        self.assertEqual(self.fresh(p).status, rules.REQUESTED)
        self.client.post(self.purl('permit_action', p.pk), {**all_ticked, 'extra': 'Fire blanket over the till'})
        p = self.fresh(p)
        self.assertEqual((p.status, p.approved_by, p.ball_in_court_user), (rules.APPROVED, self.pat, self.sam))
        self.assertEqual(p.precautions, rules.precautions_for(rules.HOT_WORKS))
        self.assertEqual(p.extra_precautions, 'Fire blanket over the till')
        self.assertEqual(p.due_date, timezone.localdate(p.expires_at))
        self.assertTrue(any('approved' in m.subject for m in mail.outbox))
        self.assertEqual(p.state, 'active')

    def test_admins_approve_too(self):
        p = self.permit(kind=rules.HEIGHT)
        self.login(self.adam)
        self.client.post(self.purl('permit_action', p.pk),
                         {'action': 'approve', 'precautions': rules.precautions_for(rules.HEIGHT)})
        self.assertEqual(self.fresh(p).status, rules.APPROVED)

    def test_a_permit_that_has_already_run_out_cant_be_approved(self):
        p = self.permit(starts=timezone.now() - datetime.timedelta(hours=10), hours=8)
        self.login(self.pat)
        r = self.client.post(self.purl('permit_action', p.pk),
                             {'action': 'approve', 'precautions': rules.precautions_for(rules.HOT_WORKS)}, follow=True)
        self.assertContains(r, 'already run out')
        self.assertEqual(self.fresh(p).status, rules.REQUESTED)

    def test_refuse_needs_a_reason(self):
        p = self.permit()
        self.login(self.pat)
        r = self.client.post(self.purl('permit_action', p.pk), {'action': 'refuse'}, follow=True)
        self.assertContains(r, 'Say why the permit is refused')
        self.client.post(self.purl('permit_action', p.pk), {'action': 'refuse', 'reason': 'No extinguisher on site'})
        p = self.fresh(p)
        self.assertEqual((p.status, p.refusal_reason, p.ball_in_court_user), (rules.REFUSED, 'No extinguisher on site',
                                                                              None))

    def approved(self, **kw):
        p = self.permit(**kw)
        with org_context(self.org_a):
            from core.workflow import apply_transition
            apply_transition(p, 'approve', self.pat, precautions=rules.precautions_for(p.permit_type))
        return self.fresh(p)

    def test_close_hot_works_needs_the_fire_watch(self):
        p = self.approved()
        self.login(self.sam)
        r = self.client.post(self.purl('permit_action', p.pk), {'action': 'close'}, follow=True)
        self.assertContains(r, 'Confirm the area has been left safe')
        r = self.client.post(self.purl('permit_action', p.pk), {'action': 'close', 'safe': 'on'}, follow=True)
        self.assertContains(r, 'Keep the fire watch for 60 minutes')
        self.client.post(self.purl('permit_action', p.pk),
                         {'action': 'close', 'safe': 'on', 'fire_watch': 'on', 'note': 'All clear 16:30'})
        p = self.fresh(p)
        self.assertEqual((p.status, p.closed_by, p.fire_watch_minutes, p.close_note),
                         (rules.CLOSED, self.sam, 60, 'All clear 16:30'))

    def test_close_other_permits_needs_only_the_area_safe(self):
        p = self.approved(kind=rules.ISOLATION)
        self.login(self.pat)
        self.client.post(self.purl('permit_action', p.pk), {'action': 'close', 'safe': 'on'})
        p = self.fresh(p)
        self.assertEqual((p.status, p.fire_watch_minutes), (rules.CLOSED, None))

    def test_withdraw_and_edit_only_while_waiting(self):
        p = self.permit()
        self.login(self.sam)
        self.assertEqual(self.client.get(self.purl('permit_edit', p.pk)).status_code, 200)
        self.client.post(self.purl('permit_action', p.pk), {'action': 'withdraw'})
        self.assertEqual(self.fresh(p).status, rules.WITHDRAWN)
        r = self.client.get(self.purl('permit_edit', p.pk), follow=True)
        self.assertContains(r, 'can&#x27;t be changed')

    def test_state_from_the_clock_and_the_banner(self):
        p = self.approved(starts=timezone.now() - datetime.timedelta(hours=7), hours=8)
        self.assertEqual(p.state, 'expiring')
        self.login(self.pat)
        self.assertContains(self.client.get(p.get_absolute_url()), 'Close it when the work is done')
        q = self.approved(kind=rules.HEIGHT, starts=timezone.now() - datetime.timedelta(hours=30), hours=36)
        with org_context(self.org_a):
            Permit.objects.filter(pk=q.pk).update(expires_at=timezone.now() - datetime.timedelta(minutes=5))
        r = self.client.get(q.get_absolute_url())
        self.assertContains(r, 'Work under it must stop')
        r = self.client.get(self.purl('permit_list') + '?status=expired')
        self.assertContains(r, q.ref)
        self.assertNotContains(r, p.ref)

    def test_print_page(self):
        p = self.approved()
        self.login(self.sam)
        r = self.client.get(self.purl('permit_print', p.pk))
        self.assertContains(r, 'Permit to work: Hot works')
        self.assertContains(r, 'fire watch')
        self.assertContains(r, 'data-print')
        waiting = self.permit()
        self.assertContains(self.client.get(self.purl('permit_print', waiting.pk)), 'NOT APPROVED')

    def test_list_csv_and_filters(self):
        self.permit(title='=SUM(A1)')
        self.approved(kind=rules.HEIGHT, title='Pendants')
        self.login(self.pat)
        r = self.client.get(self.purl('permit_list') + '?status=waiting')
        self.assertContains(r, 'SUM(A1)')
        self.assertNotContains(r, 'Pendants')
        body = self.client.get(self.purl('permit_list') + '?status=all&export=csv').content.decode()
        self.assertIn("'=SUM(A1)", body)
        self.assertIn('Pendants', body)
        r = self.client.get(self.purl('permit_list') + '?status=all&type=HEIGHT')
        self.assertContains(r, 'Pendants')
        self.assertNotContains(r, 'SUM(A1)')


@override_settings(MEDIA_ROOT=MEDIA)
class PermitNeedsMeTests(QualityTestBase):
    """Through actions.for_user, as the badge and the Needs me page get them (one index query, no extra)."""

    def items(self, user, org=None):
        org = org or self.org_a
        m = Membership.objects.filter(organization=org, user=user).first()
        if m is None:
            return []
        with org_context(org):
            return [i for i in actions.for_user(actions.request_for(user, m)) if i.kind == 'permit']

    def approve(self, p, by):
        with org_context(self.org_a):
            from core.workflow import apply_transition
            apply_transition(p, 'approve', by, precautions=rules.precautions_for(p.permit_type))
        return self.fresh(p)

    def runs_out(self, p, when):
        with org_context(self.org_a):
            p = Permit.objects.get(pk=p.pk)
            p.expires_at = when
            p.save(actor=self.pat)
        return p

    def test_waiting_permits_go_to_the_approver(self):
        p = self.permit()
        self.assertEqual([(i.ref, i.action) for i in self.items(self.pat)], [(p.ref, 'Approve or refuse the permit')])
        self.assertEqual(self.items(self.sam), [])
        self.assertEqual(self.items(self.alice), [])

    def test_approved_permits_go_to_whoever_asked_on_the_day_they_run_out(self):
        today = timezone.localdate()
        end_of_today = timezone.make_aware(datetime.datetime.combine(today, datetime.time(23, 59)))
        p = self.approve(self.permit(kind=rules.HEIGHT, starts=end_of_today - datetime.timedelta(days=2), hours=60),
                         self.pat)
        self.runs_out(p, end_of_today + datetime.timedelta(days=1))
        self.assertEqual(self.items(self.sam), [])  # runs out tomorrow: not yet
        self.runs_out(p, end_of_today)
        item = self.items(self.sam)[0]
        self.assertEqual((item.ref, item.status_label, item.due), (p.ref, 'Runs out today', today))
        self.runs_out(p, end_of_today - datetime.timedelta(days=1))
        item = self.items(self.sam)[0]
        self.assertEqual((item.status_label, item.tone), ('Expired', 'bad'))
        self.assertIn('Close the permit', item.action)
        for user in (self.pat, self.alice):  # the approver and others: not theirs
            self.assertEqual(self.items(user), [], user)

    def test_closed_permits_drop_off(self):
        p = self.approve(self.permit(kind=rules.ISOLATION), self.pat)  # in date when approved, then it lapses
        p = self.runs_out(p, timezone.now() - datetime.timedelta(days=1))
        self.assertEqual(len(self.items(self.sam)), 1)
        with org_context(self.org_a):
            from core.workflow import apply_transition
            apply_transition(p, 'close', self.sam)
        self.assertEqual(self.items(self.sam), [])

    def test_on_the_needs_me_page_and_not_twice(self):
        self.permit()
        self.login(self.pat)
        r = self.client.get(self.url('needs_me'))
        self.assertContains(r, 'Approve or refuse the permit')
        self.assertContains(r, 'PTW-001', count=1)
        self.assertContains(self.client.get(self.url('needs_me') + '?kind=permit'), 'PTW-001')

    def test_externals_and_staff_off_the_job_get_nothing(self):
        self.permit()
        for user in (self.carla, self.sid, self.connie, self.stan, self.eve, self.bea):
            self.assertEqual(self.items(user), [], user)

    def test_another_company_and_no_company(self):
        self.permit()
        Membership.objects.create(organization=self.org_b, user=self.pat, role=Membership.ROLE_ADMIN)
        self.assertEqual(self.items(self.pat, org=self.org_b), [])
        self.assertEqual(len(self.items(self.pat)), 1)
