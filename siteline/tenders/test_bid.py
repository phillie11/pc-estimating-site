"""
Bid discipline: the bid/no-bid score, the value threshold, every route to a stage going
through the gate, decline reasons, loss reviews and their analytics.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone

from directory.models import Package, Subcontractor
from projects.analytics import tender_analytics
from projects.models import Project, ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership
from tenancy.tests import Base, make_user

from . import gate, scoring
from .models import BidReview, TenderSettings

ALL = {k: 4 for k in scoring.QUESTION_KEYS}


def answers(**kw):
    return {k: kw.get(k) for k in scoring.QUESTION_KEYS}


class ScoringTableTests(SimpleTestCase):
    """Fixed table. If a weight or band moves, this says exactly what changed."""
    TABLE = [
        # client sector value programme workload terms -> score, result
        ((4, 4, 4, 4, 4, 4), 100, scoring.GO),
        ((0, 0, 0, 0, 0, 0), 0, scoring.NO_GO),
        ((2, 2, 2, 2, 2, 2), 50, scoring.REVIEW),
        ((4, 4, 4, 0, 0, 4), 70, scoring.GO),
        ((4, 4, 2, 0, 0, 4), 63, scoring.REVIEW),   # 62.5 rounds half up
        ((3, 2, 2, 2, 0, 2), 48, scoring.REVIEW),   # 47.5 rounds half up
        ((2, 2, 2, 2, 2, 0), 40, scoring.NO_GO),
        ((4, 0, 0, 0, 0, 0), 20, scoring.NO_GO),
        ((4, 4, 0, 4, 0, 4), 70, scoring.GO),
    ]

    def test_table(self):
        for points, expected, result in self.TABLE:
            a = dict(zip(scoring.QUESTION_KEYS, points))
            with self.subTest(points=points):
                self.assertEqual(scoring.score(a), expected)
                self.assertEqual(scoring.result(scoring.score(a)), result)

    def test_weights_add_to_100(self):
        self.assertEqual(sum(q.weight for q in scoring.QUESTIONS), 100)
        self.assertEqual([q.key for q in scoring.QUESTIONS],
                         ['client', 'sector', 'value', 'programme', 'workload', 'terms'])

    def test_band_edges(self):
        self.assertEqual([scoring.result(s) for s in (65, 64, 45, 44, None)],
                         [scoring.GO, scoring.REVIEW, scoring.REVIEW, scoring.NO_GO, ''])

    def test_unanswered_gives_no_score_and_bad_points_raise(self):
        self.assertIsNone(scoring.score(answers(client=4)))
        with self.assertRaises(ValueError):
            scoring.score({**ALL, 'terms': 5})

    def test_workload_and_terms_suggestions(self):
        self.assertEqual([scoring.workload_points(n) for n in range(7)], [4, 4, 3, 2, 1, 0, 0])
        self.assertEqual(scoring.terms_points('BESPOKE', '', 2), 0)
        self.assertEqual(scoring.terms_points('JCT_IC', '', 8), 0)
        self.assertEqual(scoring.terms_points('JCT_IC', 'LADs £5k/week', 3), 2)
        self.assertEqual(scoring.terms_points('NEC4_A', '', 5), 2)
        self.assertEqual(scoring.terms_points('JCT_MW', '', 3), 4)
        self.assertIsNone(scoring.terms_points('', '', None))

    def test_price_gap(self):
        self.assertEqual(scoring.price_gap(Decimal('104100'), Decimal('100000')), Decimal('4.1'))
        self.assertEqual(scoring.price_gap(Decimal('98000'), Decimal('100000')), Decimal('-2.0'))
        self.assertEqual(scoring.price_gap(Decimal('100050'), Decimal('100000')), Decimal('0.1'))  # 0.05 half up
        self.assertIsNone(scoring.price_gap(Decimal('1'), None))
        self.assertIsNone(scoring.price_gap(None, Decimal('1')))


class BidBase(Base):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.pm = make_user('pm@a.test', 'Pat PM')
        self.est = make_user('est@a.test', 'Eve Estimator')
        Membership.objects.create(organization=self.org_a, user=self.pm, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        Membership.objects.create(organization=self.org_a, user=self.est, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_ESTIMATING)
        with org_context(self.org_a):
            self.lead = Project.objects.create(name='Big lead', stage=Project.STAGE_LEAD, value=Decimal('400000'),
                                               owner=self.alice, client_name='Acme Retail')
            self.small = Project.objects.create(name='Small lead', stage=Project.STAGE_LEAD, value=Decimal('60000'),
                                                owner=self.alice)
            # The default is the check on every bid; Alpha has raised it so the optional path can be tested
            self.settings_a = TenderSettings.objects.create(gate_threshold=Decimal('250000'))

    def stage(self, project, stage, **extra):
        return self.client.post(self.url('project_stage', self.org_a, project.pk), {'stage': stage, **extra})

    def gate_post(self, project, action='go', to='TENDERING', **fields):
        data = {'action': action, 'to': to, 'from': self.reload(project).stage,
                **{f'q_{k}': v for k, v in fields.pop('q', {}).items()}, **fields}
        return self.client.post(self.url('bid_gate', self.org_a, project.pk) + f'?to={to}', data)

    def reload(self, project):
        return Project.all_objects.get(pk=project.pk)


class GateThresholdTests(BidBase):
    def test_default_is_every_bid_including_no_value(self):
        with org_context(self.org_b):
            self.assertEqual(TenderSettings.for_org(self.org_b).gate_threshold, Decimal('0'))
            for value in (Decimal('1'), Decimal('60000'), None):
                p = Project.objects.create(name=f'B lead {value}', stage=Project.STAGE_LEAD, value=value)
                self.assertTrue(gate.gate_required(p))

    def test_raised_threshold_per_organisation(self):
        with org_context(self.org_a):
            self.assertTrue(gate.gate_required(self.lead))
            self.assertFalse(gate.gate_required(self.small))
            no_value = Project.objects.create(name='No value', stage=Project.STAGE_LEAD)
            # No value and no budget: its size isn't known, so it can't be skipped
            self.assertTrue(gate.gate_required(no_value))
            self.assertFalse(gate.gate_required(no_value, BidReview(project=no_value, client_budget=Decimal('1000'))))
            self.assertTrue(gate.gate_required(no_value, BidReview(project=no_value, client_budget=Decimal('250000'))))
            TenderSettings.objects.filter(pk=self.settings_a.pk).update(gate_threshold=Decimal('50000'))
            self.assertTrue(gate.gate_required(self.small))

    def test_settings_page_admin_only(self):
        self.login(self.est)
        self.assertEqual(self.client.get(self.url('tender_settings', self.org_a)).status_code, 403)
        self.login(self.alice)
        r = self.client.post(self.url('tender_settings', self.org_a), {'gate_threshold': '100000', 'min_quotes': '4'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            s = TenderSettings.objects.get()
            self.assertEqual((s.gate_threshold, s.min_quotes), (Decimal('100000'), 4))
        r = self.client.post(self.url('tender_settings', self.org_a), {'gate_threshold': '-1', 'min_quotes': '0'})
        self.assertEqual(r.status_code, 200)


class EveryRouteHitsTheGateTests(BidBase):
    """Lead to Tendering by any route is asked the questions first, like a win goes through the handover."""

    def setUp(self):
        super().setUp()
        self.login(self.alice)
        self.gate = gate.gate_url(self.lead, 'TENDERING')

    def test_pipeline_drag(self):
        r = self.client.post(self.url('project_stage', self.org_a, self.lead.pk), json.dumps({'stage': 'TENDERING'}),
                             content_type='application/json')
        self.assertEqual(r.json(), {'ok': True, 'redirect': self.gate})
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)

    def test_move_to_control(self):
        r = self.stage(self.lead, 'TENDERING', next='pipeline')
        self.assertRedirects(r, self.gate, fetch_redirect_response=False)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)

    def test_stage_select(self):
        self.assertRedirects(self.stage(self.lead, 'TENDERING'), self.gate, fetch_redirect_response=False)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)

    def test_lead_straight_to_submitted_is_gated_too(self):
        self.assertRedirects(self.stage(self.lead, 'SUBMITTED'), gate.gate_url(self.lead, 'SUBMITTED'),
                             fetch_redirect_response=False)

    def test_edit_form_saves_other_fields_and_holds_the_stage(self):
        r = self.client.post(self.url('project_edit', self.org_a, self.lead.pk),
                             {'name': 'Big lead renamed', 'stage': 'TENDERING', 'value': '400000', 'owner': self.alice.pk})
        self.assertRedirects(r, self.gate, fetch_redirect_response=False)
        p = self.reload(self.lead)
        self.assertEqual((p.name, p.stage), ('Big lead renamed', Project.STAGE_LEAD))

    def test_creating_straight_into_tendering(self):
        r = self.client.post(self.url('project_create', self.org_a), {'name': 'Straight in', 'stage': 'TENDERING',
                                                                      'value': '500000'})
        p = Project.all_objects.get(name='Straight in')
        self.assertEqual(p.stage, Project.STAGE_LEAD)
        self.assertRedirects(r, gate.gate_url(p, 'TENDERING'), fetch_redirect_response=False)

    def test_creating_as_declined_or_lost_asks_why(self):
        r = self.client.post(self.url('project_create', self.org_a), {'name': 'Never mind', 'stage': 'DECLINED'})
        p = Project.all_objects.get(name='Never mind')
        self.assertEqual(p.stage, Project.STAGE_LEAD)
        self.assertRedirects(r, gate.decline_url(p), fetch_redirect_response=False)

    def test_decline_and_lost_by_every_route(self):
        for route in ('select', 'edit', 'json'):
            for stage, url in ((Project.STAGE_DECLINED, gate.decline_url(self.lead)),
                               (Project.STAGE_LOST, gate.lost_url(self.lead))):
                with self.subTest(route=route, stage=stage):
                    if route == 'select':
                        r = self.stage(self.lead, stage)
                        self.assertRedirects(r, url, fetch_redirect_response=False)
                    elif route == 'edit':
                        r = self.client.post(self.url('project_edit', self.org_a, self.lead.pk),
                                             {'name': 'Big lead', 'stage': stage, 'value': '400000'})
                        self.assertRedirects(r, url, fetch_redirect_response=False)
                    else:
                        r = self.client.post(self.url('project_stage', self.org_a, self.lead.pk),
                                             json.dumps({'stage': stage}), content_type='application/json')
                        self.assertEqual(r.json()['redirect'], url)
                    self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)

    def test_once_decided_moving_back_and_forward_goes_straight_through(self):
        with org_context(self.org_a):
            BidReview.objects.create(project=self.lead, decision=BidReview.PROCEED)
        self.stage(self.lead, 'TENDERING')
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_TENDERING)
        self.stage(self.lead, 'LEAD')
        self.stage(self.lead, 'SUBMITTED')
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_SUBMITTED)

    def test_reopening_a_declined_bid_asks_again(self):
        with org_context(self.org_a):
            BidReview.objects.create(project=self.lead, decision=BidReview.PROCEED)
            Project.objects.filter(pk=self.lead.pk).update(stage=Project.STAGE_DECLINED)
        self.assertRedirects(self.stage(self.lead, 'TENDERING'), self.gate, fetch_redirect_response=False)

    def test_other_moves_are_untouched(self):
        with org_context(self.org_a):
            Project.objects.filter(pk=self.lead.pk).update(stage=Project.STAGE_TENDERING)
            BidReview.objects.create(project=self.lead, decision=BidReview.PROCEED)  # already through the gate
        self.stage(self.lead, 'SUBMITTED')
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_SUBMITTED)
        r = self.stage(self.lead, 'WON')
        self.assertRedirects(r, self.url('project_handover', self.org_a, self.lead.pk), fetch_redirect_response=False)


class GatePageTests(BidBase):
    def setUp(self):
        super().setUp()
        self.login(self.alice)

    def test_mandatory_cannot_be_skipped_or_half_answered(self):
        r = self.gate_post(self.lead, action='skip')
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)
        r = self.gate_post(self.lead, q={'client': 4})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['form'].errors)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)
        r = self.client.get(self.url('bid_gate', self.org_a, self.lead.pk) + '?to=TENDERING')
        self.assertNotContains(r, 'Skip the check')

    def test_go_scores_records_and_moves(self):
        r = self.gate_post(self.lead, q={'client': 4, 'sector': 4, 'value': 4, 'programme': 2, 'workload': 4,
                                         'terms': 2}, contract_form='JCT_IC', bidders='4', amendments='LADs')
        self.assertRedirects(r, self.url('tender', self.org_a, self.lead.pk), fetch_redirect_response=False)
        p = self.reload(self.lead)
        self.assertEqual(p.stage, Project.STAGE_TENDERING)
        review = BidReview.all_objects.get(project=p)
        self.assertEqual((review.score, review.result, review.decision), (83, scoring.GO, BidReview.PROCEED))
        self.assertEqual((review.contract_form, review.bidders, review.assessed_by), ('JCT_IC', 4, self.alice))
        self.assertEqual(review.workload_bids, 0)

    def test_no_go_needs_a_reason_to_bid_anyway(self):
        zero = {k: 0 for k in scoring.QUESTION_KEYS}
        r = self.gate_post(self.lead, q=zero)
        self.assertEqual(r.status_code, 200)
        self.assertIn('decision_note', r.context['form'].errors)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)
        self.gate_post(self.lead, q=zero, decision_note='Strategic client, first framework call-off')
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_TENDERING)
        self.assertEqual(BidReview.all_objects.get(project=self.lead).result, scoring.NO_GO)

    def test_below_threshold_can_be_skipped(self):
        r = self.client.get(self.url('bid_gate', self.org_a, self.small.pk) + '?to=TENDERING')
        self.assertContains(r, 'Skip the check')
        self.gate_post(self.small, action='skip')
        self.assertEqual(self.reload(self.small).stage, Project.STAGE_TENDERING)
        self.assertEqual(BidReview.all_objects.get(project=self.small).decision, BidReview.SKIPPED)

    def test_client_budget_given_on_the_page_makes_it_mandatory(self):
        with org_context(self.org_a):
            p = Project.objects.create(name='No value yet', stage=Project.STAGE_LEAD)
        self.gate_post(p, action='skip', client_budget='900000')
        self.assertEqual(self.reload(p).stage, Project.STAGE_LEAD)

    def test_workload_is_prefilled_from_overlapping_bids(self):
        today = self.today
        with org_context(self.org_a):
            Project.objects.filter(pk=self.lead.pk).update(tender_received=today, tender_due=timezone.now() + timedelta(days=14))
            for i in range(3):  # three of Alice's bids over the same weeks
                Project.objects.create(name=f'Busy {i}', stage=Project.STAGE_TENDERING, owner=self.alice,
                                       tender_received=today - timedelta(days=3), tender_due=timezone.now() + timedelta(days=7))
            Project.objects.create(name='Later', stage=Project.STAGE_TENDERING, owner=self.alice,
                                   tender_received=today + timedelta(days=60), tender_due=timezone.now() + timedelta(days=80))
            Project.objects.create(name='Someone else', stage=Project.STAGE_TENDERING, owner=self.est,
                                   tender_received=today, tender_due=timezone.now() + timedelta(days=7))
            Project.objects.create(name='Already won', stage=Project.STAGE_WON, owner=self.alice,
                                   tender_received=today, tender_due=timezone.now() + timedelta(days=7))
            self.assertEqual(gate.overlapping_bids(self.reload(self.lead)), 3)
        r = self.client.get(self.url('bid_gate', self.org_a, self.lead.pk) + '?to=TENDERING')
        self.assertEqual(r.context['form']['q_workload'].initial, str(scoring.workload_points(3)))
        self.assertContains(r, '3 other open bids')

    def test_client_suggested_from_past_work(self):
        with org_context(self.org_a):
            Project.objects.create(name='Old job', client_name='acme retail', stage=Project.STAGE_COMPLETE)
        r = self.client.get(self.url('bid_gate', self.org_a, self.lead.pk) + '?to=TENDERING')
        self.assertEqual(r.context['form']['q_client'].initial, '4')

    def test_rescore_without_moving(self):
        with org_context(self.org_a):
            Project.objects.filter(pk=self.lead.pk).update(stage=Project.STAGE_TENDERING)
        r = self.client.post(self.url('bid_gate', self.org_a, self.lead.pk),
                             {'action': 'save', 'from': 'TENDERING', **{f'q_{k}': 2 for k in scoring.QUESTION_KEYS}})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(BidReview.all_objects.get(project=self.lead).score, 50)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_TENDERING)


NO_GO = {k: 0 for k in scoring.QUESTION_KEYS}
GO = {k: 4 for k in scoring.QUESTION_KEYS}


class ReviewRegressionTests(BidBase):
    """Defects found in independent review (PR #11), one test each."""

    def setUp(self):
        super().setUp()
        self.login(self.alice)

    def test_1_create_as_lost_or_declined_is_plan_checked_on_the_lead_it_saves(self):
        from unittest import mock
        for stage in ('LOST', 'DECLINED', 'TENDERING'):
            with self.subTest(stage=stage), mock.patch.object(Project, 'plan_has_room', return_value=False):
                r = self.client.post(self.url('project_create', self.org_a), {'name': f'Over {stage}', 'stage': stage})
                self.assertEqual(r.status_code, 200)
                self.assertFalse(Project.all_objects.filter(name=f'Over {stage}').exists())

    def test_2_save_button_while_moving_obeys_the_no_go_rule(self):
        r = self.gate_post(self.lead, action='save', q=NO_GO)
        self.assertEqual(r.status_code, 200)
        self.assertIn('decision_note', r.context['form'].errors)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)

    def test_3_skipped_bid_is_regated_when_its_value_goes_over_the_line(self):
        with org_context(self.org_a):
            p = Project.objects.create(name='Small then big', stage=Project.STAGE_LEAD, value=Decimal('10000'),
                                       owner=self.alice)
        self.gate_post(p, action='skip')
        self.assertEqual(self.reload(p).stage, Project.STAGE_TENDERING)
        # Value raised on the edit form: saved, then sent to the questions
        r = self.client.post(self.url('project_edit', self.org_a, p.pk),
                             {'name': 'Small then big', 'stage': 'TENDERING', 'value': '9000000', 'owner': self.alice.pk})
        self.assertTrue(r.url.startswith(self.url('bid_gate', self.org_a, p.pk)))
        self.assertEqual(self.reload(p).value, Decimal('9000000'))
        # Moving to Submitted, by the edit form or the stage select, is held until it is scored
        r = self.client.post(self.url('project_edit', self.org_a, p.pk),
                             {'name': 'Small then big', 'stage': 'SUBMITTED', 'value': '9000000', 'owner': self.alice.pk})
        self.assertEqual(r.url, gate.gate_url(p, 'SUBMITTED'))
        self.assertRedirects(self.stage(p, 'SUBMITTED'), gate.gate_url(p, 'SUBMITTED'), fetch_redirect_response=False)
        self.assertEqual(self.reload(p).stage, Project.STAGE_TENDERING)
        # It can't be skipped again; answering it lets it through
        self.client.post(self.url('bid_gate', self.org_a, p.pk), {'action': 'skip', 'from': 'TENDERING'})
        self.assertEqual(BidReview.all_objects.get(project=p).decision, BidReview.SKIPPED)
        self.client.post(self.url('bid_gate', self.org_a, p.pk),
                         {'action': 'save', 'from': 'TENDERING', **{f'q_{k}': v for k, v in GO.items()}})
        self.assertEqual(BidReview.all_objects.get(project=p).decision, BidReview.PROCEED)
        self.stage(p, 'SUBMITTED')
        self.assertEqual(self.reload(p).stage, Project.STAGE_SUBMITTED)

    def test_3c_tendering_with_no_decision_is_gated_before_submitted(self):
        """Owner decision: always mandatory, so a bid tendering from before the gate is asked at Submitted."""
        with org_context(self.org_a):
            p = Project.objects.create(name='Old tender', stage=Project.STAGE_TENDERING, value=Decimal('400000'),
                                       owner=self.alice)
        url = gate.gate_url(p, 'SUBMITTED')
        self.assertRedirects(self.stage(p, 'SUBMITTED'), url, fetch_redirect_response=False)
        r = self.client.post(self.url('project_stage', self.org_a, p.pk), json.dumps({'stage': 'SUBMITTED'}),
                             content_type='application/json')
        self.assertEqual(r.json()['redirect'], url)
        r = self.client.post(self.url('project_edit', self.org_a, p.pk),
                             {'name': 'Old tender', 'stage': 'SUBMITTED', 'value': '400000', 'owner': self.alice.pk})
        self.assertEqual(r.url, url)
        self.assertEqual(self.reload(p).stage, Project.STAGE_TENDERING)
        # The gate page itself makes the move once answered
        self.gate_post(p, to='SUBMITTED', q=GO)
        self.assertEqual(self.reload(p).stage, Project.STAGE_SUBMITTED)
        self.assertEqual(BidReview.all_objects.get(project=p).decision, BidReview.PROCEED)

    def test_3b_no_value_cannot_be_skipped(self):
        with org_context(self.org_a):
            p = Project.objects.create(name='No value', stage=Project.STAGE_LEAD, owner=self.alice)
        self.gate_post(p, action='skip')
        self.assertEqual(self.reload(p).stage, Project.STAGE_LEAD)

    def test_4_lost_then_lead_then_tendering_asks_again(self):
        self.gate_post(self.lead, q=GO)
        self.client.post(self.url('bid_lost', self.org_a, self.lead.pk), {'loss_reason': 'PRICE'})
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LOST)
        self.stage(self.lead, 'LEAD')
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)
        self.assertRedirects(self.stage(self.lead, 'TENDERING'), gate.gate_url(self.lead, 'TENDERING'),
                             fetch_redirect_response=False)

    def test_5_reopening_a_declined_bid_records_a_bid_and_clears_the_decline(self):
        self.client.post(self.url('bid_decline', self.org_a, self.lead.pk), {'decline_reason': 'BUSY'})
        self.gate_post(self.lead, action='save', q=GO)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_TENDERING)
        r = BidReview.all_objects.get(project=self.lead)
        self.assertEqual((r.decision, r.decline_reason, r.decline_detail), (BidReview.PROCEED, '', ''))

    def test_6_decline_and_lost_refuse_jobs_that_are_not_open_bids(self):
        with org_context(self.org_a):
            done = Project.objects.create(name='Done', stage=Project.STAGE_COMPLETE)
        for project, stage in ((self.pa, Project.STAGE_LIVE), (done, Project.STAGE_COMPLETE)):
            for name, data in (('bid_decline', {'decline_reason': 'BUSY'}), ('bid_lost', {'loss_reason': 'PRICE'})):
                with self.subTest(project=project.name, page=name):
                    r = self.client.post(self.url(name, self.org_a, project.pk), data)
                    self.assertRedirects(r, project.get_absolute_url(), fetch_redirect_response=False)
                    self.assertEqual(self.reload(project).stage, stage)
        # Editing the reason on one already declined is still fine
        self.client.post(self.url('bid_decline', self.org_a, self.lead.pk), {'decline_reason': 'BUSY'})
        r = self.client.post(self.url('bid_decline', self.org_a, self.lead.pk), {'decline_reason': 'TERMS'})
        self.assertEqual(BidReview.all_objects.get(project=self.lead).decline_reason, 'TERMS')

    def test_9_stale_gate_page_is_refused(self):
        page = self.client.get(self.url('bid_gate', self.org_a, self.lead.pk) + '?to=TENDERING')
        self.assertContains(page, 'name="from" value="LEAD"')
        with org_context(self.org_a):  # someone else declines it meanwhile
            Project.objects.filter(pk=self.lead.pk).update(stage=Project.STAGE_DECLINED)
        r = self.client.post(self.url('bid_gate', self.org_a, self.lead.pk) + '?to=TENDERING',
                             {'action': 'go', 'to': 'TENDERING', 'from': 'LEAD', **{f'q_{k}': 4 for k in GO}},
                             follow=True)
        self.assertContains(r, 'nothing was saved')
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_DECLINED)
        self.assertFalse(BidReview.all_objects.filter(project=self.lead, score__isnull=False).exists())

    def test_direct_award_is_recorded(self):
        with org_context(self.org_a):
            pm = Membership.objects.get(user=self.pm)
            self.assertTrue(pm.can_deliver)
        r = self.client.post(self.url('project_handover', self.org_a, self.small.pk),
                             {'manager': self.pm.pk, 'value': '60000'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.reload(self.small).stage, Project.STAGE_WON)
        self.assertEqual(BidReview.all_objects.get(project=self.small).decision, BidReview.DIRECT_AWARD)
        # A bid that went through the gate and was then won keeps its decision
        with org_context(self.org_a):
            Project.objects.filter(pk=self.lead.pk).update(stage=Project.STAGE_SUBMITTED)
            BidReview.objects.create(project=self.lead, decision=BidReview.PROCEED)
        self.client.post(self.url('project_handover', self.org_a, self.lead.pk), {'manager': self.pm.pk, 'value': '1'})
        self.assertEqual(BidReview.all_objects.get(project=self.lead).decision, BidReview.PROCEED)
        with org_context(self.org_a):
            rows = {r.label: r.bids for r in tender_analytics(Project.objects.all(), '12', today=self.today)['by_score']}
        self.assertEqual(rows.get('Direct award'), 1)


class DeclineAndLossTests(BidBase):
    def setUp(self):
        super().setUp()
        self.login(self.alice)

    def test_decline_reason_required(self):
        url = self.url('bid_decline', self.org_a, self.lead.pk)
        r = self.client.post(url, {})
        self.assertEqual(r.status_code, 200)
        self.assertIn('decline_reason', r.context['form'].errors)
        r = self.client.post(url, {'decline_reason': 'NONSENSE'})
        self.assertIn('decline_reason', r.context['form'].errors)
        r = self.client.post(url, {'decline_reason': 'OTHER'})
        self.assertIn('decline_detail', r.context['form'].errors)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)

    def test_decline_moves_records_and_closes_tenders(self):
        with org_context(self.org_a):
            sub = Subcontractor.objects.create(company_name='Dry Co', email='d@d.test')
            pkg = Package.objects.create(project=self.lead, subcontractor=sub, status=Package.ENQUIRY)
        r = self.client.post(self.url('bid_decline', self.org_a, self.lead.pk),
                             {'decline_reason': 'BIDDERS', 'decline_detail': 'Eight on the list'})
        self.assertRedirects(r, self.lead.get_absolute_url(), fetch_redirect_response=False)
        p = self.reload(self.lead)
        self.assertEqual(p.stage, Project.STAGE_DECLINED)
        self.assertIn('Too many bidders: Eight on the list', p.outcome_notes)
        review = BidReview.all_objects.get(project=p)
        self.assertEqual((review.decline_reason, review.decision), ('BIDDERS', BidReview.DECLINED))
        self.assertEqual(Package.all_objects.get(pk=pkg.pk).status, Package.UNSUCCESSFUL)

    def test_loss_review_required_and_gap_worked_out(self):
        with org_context(self.org_a):
            Project.objects.filter(pk=self.lead.pk).update(stage=Project.STAGE_SUBMITTED)
        url = self.url('bid_lost', self.org_a, self.lead.pk)
        r = self.client.get(url)
        self.assertEqual(r.context['form']['our_price'].initial, Decimal('400000.00'))
        r = self.client.post(url, {'our_price': '400000'})
        self.assertIn('loss_reason', r.context['form'].errors)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_SUBMITTED)
        self.client.post(url, {'loss_reason': 'PRICE', 'our_price': '416400', 'winner_price': '400000',
                               'lessons': 'Prelims heavy'})
        p = self.reload(self.lead)
        self.assertEqual(p.stage, Project.STAGE_LOST)
        review = BidReview.all_objects.get(project=p)
        self.assertEqual((review.gap_pct, review.loss_reviewed_by), (Decimal('4.1'), self.alice))
        self.assertIn('4.1% above the winning price', p.outcome_notes)
        # Adding the winner's price later doesn't move anything
        self.client.post(url, {'loss_reason': 'PRICE', 'our_price': '416400', 'winner_price': '410000'})
        self.assertEqual(BidReview.all_objects.get(project=p).gap_pct, Decimal('1.6'))


class RoleTests(BidBase):
    """Who can open the gate, decline and loss pages."""

    def setUp(self):
        super().setUp()
        self.client_user = make_user('cli@a.test', 'Cli Ent')
        self.bidder = make_user('bid@a.test', 'Bid Der')
        self.subu = make_user('sub@a.test', 'Sub Con')
        self.consultant = make_user('con@a.test', 'Con Sult')
        with org_context(self.org_a):
            sub = Subcontractor.objects.create(company_name='Bidder Ltd', email='bid@a.test')
            for u, role in ((self.client_user, ProjectMember.ROLE_CLIENT), (self.bidder, ProjectMember.ROLE_BIDDER),
                            (self.subu, ProjectMember.ROLE_SUBCONTRACTOR),
                            (self.consultant, ProjectMember.ROLE_CONSULTANT)):
                Membership.objects.create(organization=self.org_a, user=u, role=Membership.ROLE_EXTERNAL)
                ProjectMember.objects.create(project=self.lead, user=u, role=role,
                                             subcontractor=sub if role == ProjectMember.ROLE_BIDDER else None)
            self.admin = make_user('adm@a.test', 'Ad Min')
            Membership.objects.create(organization=self.org_a, user=self.admin, role=Membership.ROLE_ADMIN)

    def test_each_role(self):
        names = ('bid_gate', 'bid_decline', 'bid_lost')
        expected = [(self.alice, 200), (self.admin, 200), (self.est, 200), (self.pm, 403),
                    (self.client_user, 403), (self.consultant, 403), (self.subu, 403), (self.bidder, 403),
                    (self.bob, 404)]
        for user, code in expected:
            self.login(user)
            for name in names:
                with self.subTest(user=user.email, page=name):
                    self.assertEqual(self.client.get(self.url(name, self.org_a, self.lead.pk)).status_code, code)
        # And none of them could move it
        self.login(self.bidder)
        self.client.post(self.url('bid_decline', self.org_a, self.lead.pk), {'decline_reason': 'BUSY'})
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)


class TenancyTests(BidBase):
    def test_other_company_gets_404_and_no_context_fails_closed(self):
        self.login(self.bob)
        # Bob's own workspace, Alpha's project id: hidden by the tenant manager
        for name in ('bid_gate', 'bid_decline', 'bid_lost'):
            self.assertEqual(self.client.get(self.url(name, self.org_b, self.lead.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.url('bid_decline', self.org_b, self.lead.pk),
                                          {'decline_reason': 'BUSY'}).status_code, 404)
        self.assertEqual(self.reload(self.lead).stage, Project.STAGE_LEAD)
        from tenancy.context import TenantContextMissing
        with self.assertRaises(TenantContextMissing):
            list(BidReview.objects.all())
        with self.assertRaises(TenantContextMissing):
            list(TenderSettings.objects.all())

    def test_review_row_cannot_be_written_into_another_company(self):
        with org_context(self.org_b):
            with self.assertRaises(PermissionError):
                BidReview(project=self.lead, organization=self.org_a).save()


class DemoSeedTests(BidBase):
    def test_seed_demo_has_bid_discipline_data(self):
        import io
        import tempfile

        from django.core.management import call_command

        from tenancy.models import Organization

        from . import actions
        with self.settings(MEDIA_ROOT=tempfile.mkdtemp()):
            call_command('seed_demo', stdout=io.StringIO())
        demo = Organization.objects.get(slug='northbank-interiors')
        with org_context(demo):
            self.assertGreaterEqual(BidReview.objects.filter(result=scoring.GO).count(), 3)
            self.assertTrue(BidReview.objects.filter(decline_reason='BIDDERS').exists())
            self.assertTrue(BidReview.objects.filter(winner_price__isnull=False).exists())
            self.assertTrue(gate.gate_required(Project.objects.get(name='Gym fit-out, Ellesmere Port')))
            self.assertFalse(gate.gate_required(Project.objects.get(name='Barbers refit, Didsbury')))
            self.assertEqual(Subcontractor.objects.get(company_name='Brightside Electrical').region_codes, ['UK'])
            self.assertEqual([a['project'].name for a in actions.coverage_alerts_for_org(demo)],
                             ['Jewellery store, Argyle Arcade'])
        self.assertFalse(BidReview.all_objects.exclude(organization=demo).exists())


class AnalyticsTests(BidBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            def bid(name, stage, result=None, **review):
                p = Project.objects.create(name=name, stage=stage, value=Decimal('100000'),
                                           decided_at=self.today - timedelta(days=10))
                if result or review:
                    BidReview.objects.create(project=p, result=result or '', **review)
                return p
            bid('Go won 1', Project.STAGE_WON, scoring.GO)
            bid('Go won 2', Project.STAGE_COMPLETE, scoring.GO)
            bid('Go lost', Project.STAGE_LOST, scoring.GO, loss_reason='PRICE', our_price=Decimal('104100'),
                winner_price=Decimal('100000'))
            bid('No-go lost', Project.STAGE_LOST, scoring.NO_GO, loss_reason='PRICE', our_price=Decimal('102000'),
                winner_price=Decimal('100000'))
            bid('Unscored lost', Project.STAGE_LOST)
            bid('Declined busy', Project.STAGE_DECLINED, decline_reason='BUSY')
            bid('Declined busy 2', Project.STAGE_DECLINED, decline_reason='BUSY')
            bid('Declined terms', Project.STAGE_DECLINED, decline_reason='TERMS')
            bid('Declined unknown', Project.STAGE_DECLINED)

    def data(self):
        with org_context(self.org_a):
            return tender_analytics(Project.objects.all(), '12', today=self.today)

    def test_win_rate_by_score_band(self):
        rows = {r.label: (r.bids, r.won, r.win_rate) for r in self.data()['by_score']}
        self.assertEqual(rows, {'Go': (3, 2, 67), 'No-go': (1, 0, 0), 'Not scored': (2, 1, 50)})  # + the base LIVE job
        self.assertEqual([r.label for r in self.data()['by_score']], ['Go', 'No-go', 'Not scored'])

    def test_decline_reasons_breakdown(self):
        rows = [(r.label, r.count, r.pct) for r in self.data()['decline_reasons']]
        self.assertEqual(rows, [('Too busy', 2, 50), ('Onerous terms', 1, 25), ('Not recorded', 1, 25)])

    def test_loss_reviews(self):
        d = self.data()
        self.assertEqual((d['losses_total'], d['losses_reviewed'], d['price_gaps_known']), (3, 2, 2))
        self.assertEqual(d['avg_price_gap'], Decimal('3.1'))  # (4.1 + 2.0) / 2 = 3.05, half up
        self.assertEqual({r.label: r.count for r in d['loss_reasons']}, {'Price': 2, 'Not recorded': 1})

    def test_page_renders_the_new_sections(self):
        self.login(self.alice)
        r = self.client.get(self.url('tender_analytics', self.org_a))
        for text in ('By bid/no-bid score', 'Why we decline', 'Why we lose', 'Loss reviews', '+4.1%'):
            self.assertContains(r, text)
