"""The transition helper, on RFIs and through the RFI views."""
from unittest import mock

from django.core.exceptions import ImproperlyConfigured, PermissionDenied

from core.exceptions import InvalidTransition, TransitionForbidden
from core.models import AuditEntry, RecordIndex
from core.registry import get_record_type, register_record_type
from core.workflow import Transition, apply_transition, available_transitions
from rfis.models import RFI
from tenancy.context import org_context

from .base import RecordTestBase, make_user


class TransitionTests(RecordTestBase):
    def setUp(self):
        super().setUp()
        self.r = self.rfi(ball_in_court_user=self.carla)

    def audit(self):
        with org_context(self.org_a):
            return list(AuditEntry.objects.filter(record_idx__object_id=self.r.pk).values_list('action', 'changes'))

    def test_ball_in_court_can_respond_and_it_is_audited_under_the_transition_name(self):
        with org_context(self.org_a):
            apply_transition(self.r, 'respond', self.carla, response='Use smoked oak.')
            r = RFI.objects.get(pk=self.r.pk)
        self.assertEqual((r.status, r.response, r.responded_by, r.version), ('RESPONDED', 'Use smoked oak.', self.carla, 2))
        action, changes = self.audit()[-1]
        self.assertEqual(action, 'respond')
        self.assertEqual(changes['status'], ['OPEN', 'RESPONDED'])
        self.assertEqual(changes['response'], ['', 'Use smoked oak.'])

    def test_permission_and_source_status_are_checked(self):
        stranger = make_user('sid@s.test', 'Sid Sub')
        with org_context(self.org_a):
            with self.assertRaises(TransitionForbidden):
                apply_transition(self.r, 'respond', stranger, response='sneaky')
            with self.assertRaises(PermissionDenied):  # TransitionForbidden is one, so views answer 403
                apply_transition(self.r, 'close', self.carla)  # ball in court, but closing is staff only
            with self.assertRaises(InvalidTransition):
                apply_transition(self.r, 'reopen', self.alice)  # already open
            with self.assertRaises(InvalidTransition):
                apply_transition(self.r, 'approve', self.alice)  # not an RFI transition
            self.assertEqual(RFI.objects.get(pk=self.r.pk).status, 'OPEN')
        self.assertEqual([a for a, _ in self.audit()], ['created'])

    def test_available_transitions_depend_on_status_and_user(self):
        with org_context(self.org_a):
            self.assertEqual([t.name for t in available_transitions(self.r, self.carla)], ['respond'])
            self.assertEqual([t.name for t in available_transitions(self.r, self.alice)],
                             ['respond', 'respond_and_close', 'close', 'void'])

    def test_answering_passes_the_ball_back_to_the_raiser(self):
        with org_context(self.org_a):
            apply_transition(self.r, 'respond', self.carla, response='Oak')
            idx = RecordIndex.objects.get(object_id=self.r.pk)
        self.assertEqual((self.r.ball_in_court_user, idx.ball_in_court_user, idx.is_open), (self.alice, self.alice, True))
        self.assertEqual(self.audit()[-1][1]['ball_in_court_user'], [self.carla.pk, self.alice.pk])

    def test_a_transition_can_clear_or_keep_the_ball(self):
        park = Transition('park', source=('OPEN',), target='DRAFT', sets_ball_in_court=None)
        with mock.patch.dict(get_record_type(RFI).transitions, {'park': park}), org_context(self.org_a):
            apply_transition(self.r, 'park', self.alice)
        self.assertIsNone(self.r.ball_in_court_user)

    def test_registration_rejects_unknown_statuses(self):
        with self.assertRaises(ImproperlyConfigured):
            register_record_type(RFI, prefix='RFI', transitions=[Transition('x', source=('OPEN',), target='NOPE')])


class RFIViewTransitionTests(RecordTestBase):
    """The RFI views now go through apply_transition; the behaviour people see is unchanged."""

    def setUp(self):
        super().setUp()
        self.r = self.rfi(ball_in_court_user=self.carla)
        self.detail = self.url('rfi_detail', self.org_a, self.pa.pk, self.r.pk)

    def status(self):
        with org_context(self.org_a):
            return RFI.objects.get(pk=self.r.pk).status

    def test_respond_close_reopen_void(self):
        self.login(self.carla)
        self.client.post(self.detail, {'action': 'respond', 'response': 'Oak'})
        self.assertEqual(self.status(), 'RESPONDED')
        self.login(self.alice)
        self.client.post(self.detail, {'action': 'close'})
        self.assertEqual(self.status(), 'CLOSED')
        self.client.post(self.detail, {'action': 'reopen'})
        self.assertEqual(self.status(), 'OPEN')
        self.client.post(self.detail, {'action': 'void'})
        self.assertEqual(self.status(), 'VOID')
        with org_context(self.org_a):
            r = RFI.objects.get(pk=self.r.pk)
            actions = list(AuditEntry.objects.filter(record_idx__object_id=r.pk).values_list('action', 'actor'))
        self.assertIsNotNone(r.closed_at)
        self.assertEqual(actions, [('created', self.alice.pk), ('respond', self.carla.pk), ('close', self.alice.pk),
                                   ('reopen', self.alice.pk), ('void', self.alice.pk)])

    def test_staff_respond_and_close_in_one_go(self):
        self.login(self.alice)
        self.client.post(self.detail, {'action': 'respond', 'response': 'Oak', 'close': 'on'})
        self.assertEqual(self.status(), 'CLOSED')

    def test_a_transition_from_the_wrong_status_is_a_message_not_an_error(self):
        self.login(self.alice)
        r = self.client.post(self.detail, {'action': 'reopen'}, follow=True)
        self.assertContains(r, 'Cannot reopen RFI-001')
        self.assertEqual(self.status(), 'OPEN')

    def test_external_cannot_close(self):
        self.login(self.carla)
        self.client.post(self.detail, {'action': 'close'})
        self.assertEqual(self.status(), 'OPEN')

    def test_pass_the_ball_is_audited(self):
        self.login(self.alice)
        self.client.post(self.detail, {'action': 'reassign', 'ball_in_court_user': self.alice.pk})
        with org_context(self.org_a):
            e = AuditEntry.objects.filter(record_idx__object_id=self.r.pk).last()
            idx = RecordIndex.objects.get(object_id=self.r.pk)
        self.assertEqual((e.action, e.changes), ('reassigned', {'ball_in_court_user': [self.carla.pk, self.alice.pk]}))
        self.assertEqual(idx.ball_in_court_user, self.alice)

    def test_raising_through_the_form_records_who(self):
        self.login(self.alice)
        self.client.post(self.url('rfi_create', self.org_a, self.pa.pk), {
            'title': 'Ceiling void depth', 'question': 'How deep?', 'category': 'DESIGN', 'priority': 'NORMAL',
            'ball_in_court_user': self.carla.pk, 'cost_impact': 'TBC'})
        with org_context(self.org_a):
            r = RFI.objects.get(title='Ceiling void depth')
            e = AuditEntry.objects.get(record_idx__object_id=r.pk)
        self.assertEqual((r.ref, r.created_by, r.visibility, e.actor), ('RFI-002', self.alice, 'INTERNAL', self.alice))

    def test_register_csv_still_exports(self):
        self.login(self.alice)
        r = self.client.get(self.url('rfi_list', self.org_a, self.pa.pk) + '?export=csv')
        self.assertContains(r, 'RFI-001,Which veneer?')
