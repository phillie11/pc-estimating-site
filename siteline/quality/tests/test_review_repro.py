"""
Review of the Quality branch (claude/r3-quality): one test per defect found, each failing before its fix.

  1. A signed-off fire-stopping penetration could still be changed through "Change details"
     (system, rating, location), so the signed-off record no longer matched what was inspected.
  2. An approved permit could be withdrawn after work under it had started, which took it off the
     register's live list and Needs me without the closing checks (area left safe, hot-works fire watch).
  3. A penetration reopened (or failed) and sealed again could be signed off on the "after" photo of
     the earlier seal, so the sign-off had no evidence of the seal actually inspected.

Plus a guard for deletes: quality photos must stay fast-deletable, because on MySQL (no deferred
constraint checks) Django's slow path nulls a nullable CASCADE key before deleting the row, which
would break quality_photo_one_record when a company or job is deleted.
"""
import datetime
from unittest import mock

from django.db import connection
from django.db.models.deletion import Collector
from django.test import override_settings
from django.utils import timezone

from core.exceptions import InvalidTransition, TransitionForbidden
from core.workflow import apply_transition
from tenancy.context import org_context

from .. import rules
from ..models import Penetration, Permit, QualityPhoto
from ..photos import add_photo
from .base import MEDIA, QualityTestBase, jpeg, tearDownModule  # noqa: F401


@override_settings(MEDIA_ROOT=MEDIA)
class SignedOffPenetrationIsLockedTests(QualityTestBase):
    def signed_off(self):
        p = self.penetration(system='Intumescent collar', size='110 mm', service='PIPE_PLASTIC')
        with org_context(self.org_a):
            add_photo(self.pa, self.pat, jpeg(), penetration=p, stage=QualityPhoto.AFTER)
            apply_transition(p, 'seal', self.pat)
            apply_transition(p, 'sign_off', self.pat, remark='As detail')
        return self.fresh(p)

    def test_signed_off_penetration_cant_be_edited(self):
        p = self.signed_off()
        self.login(self.pat)
        r = self.client.post(self.purl('penetration_edit', p.pk), {
            'location': 'Somewhere else', 'element': 'FLOOR', 'fire_rating': '30', 'service': 'CABLE',
            'system': '', 'version': p.version}, follow=True)
        after = self.fresh(p)
        self.assertEqual((after.status, after.location, after.system, after.fire_rating),
                         (Penetration.SIGNED_OFF, 'L1 riser wall', 'Intumescent collar', 60))
        self.assertContains(r, 'signed off')

    def test_no_change_details_button_once_signed_off(self):
        p = self.signed_off()
        self.login(self.pat)
        self.assertNotContains(self.client.get(p.get_absolute_url()), self.purl('penetration_edit', p.pk))
        self.assertEqual(self.client.get(self.purl('penetration_edit', p.pk)).status_code, 302)


@override_settings(MEDIA_ROOT=MEDIA)
class PermitWithdrawTests(QualityTestBase):
    def approved(self, **kw):
        p = self.permit(**kw)
        with org_context(self.org_a):
            apply_transition(p, 'approve', self.pat, precautions=rules.precautions_for(p.permit_type))
        return self.fresh(p)

    def test_started_hot_works_permit_cant_be_withdrawn_to_skip_the_fire_watch(self):
        p = self.approved()  # started an hour ago
        self.login(self.sam)
        r = self.client.get(p.get_absolute_url())
        self.assertNotContains(r, 'value="withdraw"')
        r = self.client.post(self.purl('permit_action', p.pk), {'action': 'withdraw'}, follow=True)
        self.assertEqual(self.fresh(p).status, rules.APPROVED)
        self.assertContains(r, 'close it')

    def test_expired_permit_cant_be_withdrawn(self):
        p = self.approved(kind=rules.ISOLATION)
        with org_context(self.org_a):
            Permit.objects.filter(pk=p.pk).update(expires_at=timezone.now() - datetime.timedelta(minutes=5))
        self.login(self.pat)
        self.client.post(self.purl('permit_action', p.pk), {'action': 'withdraw'})
        self.assertEqual(self.fresh(p).status, rules.APPROVED)

    def test_approved_permit_can_still_be_withdrawn_before_work_starts(self):
        p = self.approved(starts=timezone.now() + datetime.timedelta(hours=2))
        self.login(self.sam)
        self.assertContains(self.client.get(p.get_absolute_url()), 'value="withdraw"')
        self.client.post(self.purl('permit_action', p.pk), {'action': 'withdraw'})
        self.assertEqual(self.fresh(p).status, rules.WITHDRAWN)

    def test_waiting_permit_can_be_withdrawn(self):
        p = self.permit()
        self.login(self.sam)
        self.client.post(self.purl('permit_action', p.pk), {'action': 'withdraw'})
        self.assertEqual(self.fresh(p).status, rules.WITHDRAWN)


@override_settings(MEDIA_ROOT=MEDIA)
class ResignOffNeedsANewPhotoTests(QualityTestBase):
    def test_reopened_penetration_needs_a_new_after_photo(self):
        p = self.penetration(system='Collar')
        with org_context(self.org_a):
            add_photo(self.pa, self.pat, jpeg(), penetration=p, stage=QualityPhoto.AFTER)
            apply_transition(p, 'seal', self.pat)
            apply_transition(p, 'sign_off', self.pat)
            apply_transition(self.fresh(p), 'reopen', self.pat, remark='Cut out for a new cable')
            p = self.fresh(p)
            apply_transition(p, 'seal', self.pat)
            p = self.fresh(p)
            with self.assertRaises(InvalidTransition):
                apply_transition(p, 'sign_off', self.pat)
            p = self.fresh(p)
            add_photo(self.pa, self.pat, jpeg(), penetration=p, stage=QualityPhoto.AFTER)
            apply_transition(p, 'sign_off', self.pat)
        self.assertEqual(self.fresh(p).status, Penetration.SIGNED_OFF)

    def test_failed_then_resealed_needs_a_photo_of_the_reseal(self):
        p = self.penetration(system='Collar')
        with org_context(self.org_a):
            add_photo(self.pa, self.pat, jpeg(), penetration=p, stage=QualityPhoto.AFTER)
            apply_transition(p, 'seal', self.pat)
            apply_transition(self.fresh(p), 'fail', self.pat, remark='Gap behind the cables')
            apply_transition(self.fresh(p), 'reseal', self.sam)
            with self.assertRaises(InvalidTransition):
                apply_transition(self.fresh(p), 'sign_off', self.pat)


@override_settings(MEDIA_ROOT=MEDIA)
class InspectorMustBeAbleToOpenTheJobTests(QualityTestBase):
    """
    The bid's estimator often stays on the team after the job is won. Estimating-only staff can't open
    live jobs (projects.access.visible_projects), so an inspection given to them is on nobody's Needs me
    and they can't open it. They must not be offered as the inspector.
    """

    def test_estimating_only_team_member_is_not_offered_as_inspector(self):
        from projects.models import ProjectMember

        from ..access import on_job_staff
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.eve, role=ProjectMember.ROLE_ESTIMATOR)
            people = set(on_job_staff(self.pa))
        self.assertIn(self.sam, people)
        self.assertIn(self.pat, people)
        self.assertNotIn(self.eve, people)
        self.login(self.pat)
        r = self.client.post(self.purl('inspection_create'), {
            'template': self.template.pk, 'area': 'L1', 'ball_in_court_user': self.eve.pk,
            'due_date': timezone.localdate().isoformat()})
        self.assertEqual(r.status_code, 200)  # form error: not a valid choice
        with org_context(self.org_a):
            from ..models import Inspection
            self.assertFalse(Inspection.objects.filter(ball_in_court_user=self.eve).exists())


@override_settings(MEDIA_ROOT=MEDIA)
class CsvFormulaGuardTests(QualityTestBase):
    def test_fire_stopping_csv_cells_cant_run_as_formulas(self):
        self.penetration(location=' =HYPERLINK("http://x","y")', system='@SUM(A1)', installer='+1+1',
                         size='-2+3', detail_reference='\t=1')
        self.login(self.pat)
        body = self.client.get(self.purl('penetration_list') + '?export=csv').content.decode()
        for raw in (' =HYPERLINK', '@SUM(A1)', '+1+1', '-2+3', '\t=1'):
            self.assertIn("'" + raw, body, raw)


@override_settings(MEDIA_ROOT=MEDIA)
class DeleteGuardTests(QualityTestBase):
    def test_quality_photos_stay_fast_deletable(self):
        self.assertTrue(Collector(using='default').can_fast_delete(QualityPhoto.objects.none()),
                        'Something listens to QualityPhoto deletes; on MySQL that breaks quality_photo_one_record.')

    def test_company_with_photos_deletes_without_deferred_constraint_checks(self):
        ins, items = self.inspection()
        fs = self.penetration()
        with org_context(self.org_a):
            add_photo(self.pa, self.pat, jpeg(), item=items[0])
            add_photo(self.pa, self.pat, jpeg(), penetration=fs, stage=QualityPhoto.BEFORE)
            with mock.patch.object(connection.features, 'can_defer_constraint_checks', False):
                self.org_a.delete()  # what MySQL does: the slow path would null item/penetration first
        self.assertFalse(QualityPhoto.all_objects.exists())


@override_settings(MEDIA_ROOT=MEDIA)
class NoSelfApprovalTests(QualityTestBase):
    """
    PENDING OWNER DECISION: whoever asked for a permit must not approve (or refuse) it themselves. It goes
    to the PM, or to an owner or admin when the PM asked for it.
    """
    ticked = {'action': 'approve', 'precautions': rules.precautions_for(rules.HOT_WORKS)}

    def test_pm_cant_approve_their_own_permit_and_it_goes_to_an_admin(self):
        self.login(self.pat)
        start = timezone.localtime() + datetime.timedelta(hours=1)
        self.client.post(self.purl('permit_create'), {
            'permit_type': rules.HOT_WORKS, 'title': 'Weld bracket', 'location': 'Bar',
            'starts_at': start.strftime('%Y-%m-%dT%H:%M'),
            'expires_at': (start + datetime.timedelta(hours=4)).strftime('%Y-%m-%dT%H:%M')})
        with org_context(self.org_a):
            p = Permit.objects.get()
        self.assertEqual(p.created_by, self.pat)
        self.assertIn(p.ball_in_court_user, (self.alice, self.adam))
        r = self.client.get(p.get_absolute_url())
        self.assertNotContains(r, 'value="approve"')
        self.assertContains(r, 'someone else must approve it')
        r = self.client.post(self.purl('permit_action', p.pk), self.ticked)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.fresh(p).status, rules.REQUESTED)
        self.login(self.adam)
        self.client.post(self.purl('permit_action', p.pk), self.ticked)
        p = self.fresh(p)
        self.assertEqual((p.status, p.approved_by), (rules.APPROVED, self.adam))

    def test_admin_cant_approve_or_refuse_their_own_permit(self):
        p = self.permit(actor=self.adam)
        with org_context(self.org_a):
            with self.assertRaises(TransitionForbidden):
                apply_transition(p, 'approve', self.adam, precautions=rules.precautions_for(p.permit_type))
            with self.assertRaises(TransitionForbidden):
                apply_transition(self.fresh(p), 'refuse', self.adam, reason='Not today')
            apply_transition(self.fresh(p), 'approve', self.pat, precautions=rules.precautions_for(p.permit_type))
        self.assertEqual(self.fresh(p).status, rules.APPROVED)
