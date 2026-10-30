"""
Early warnings and change events: numbering and index, transitions through the views, who can
open them (staff only, never externals, not even through the record index), the CE -> VO and
RFI/EW links, sub variations, the registers' filters, Needs-me items, and tenancy.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from contracts.models import MainContract
from core.access import needs_me, visible_index
from core.models import AuditEntry, RecordIndex
from core.workflow import apply_transition
from delivery.models import SubVariation
from rfis.models import RFI
from tenancy.context import TenantContextMissing, org_context
from variations.models import Variation

from ..actions import items
from ..links import came_from, led_to
from ..models import ChangeEvent, ChangeEventLine, EarlyWarning
from .base import CommercialTestBase


class ChangeTestBase(CommercialTestBase):
    def ew(self, project=None, org=None, actor=None, **kw):
        kw.setdefault('title', 'Riser approval late')
        kw.setdefault('description', 'x')
        with org_context(org or self.org_a):
            e = EarlyWarning(project=project or self.pa, **kw)
            e.save(actor=actor or self.pat)
            return e

    def ce(self, project=None, org=None, actor=None, **kw):
        kw.setdefault('title', 'Extra data points')
        kw.setdefault('description', 'x')
        with org_context(org or self.org_a):
            c = ChangeEvent(project=project or self.pa, **kw)
            c.save(actor=actor or self.pat)
            return c

    def rfi(self, **kw):
        with org_context(self.org_a):
            r = RFI(project=self.pa, title=kw.pop('title', 'Floor height?'), question='?', **kw)
            r.save(actor=self.pat)
            return r


class RecordTests(ChangeTestBase):
    def test_numbering_refs_and_always_internal(self):
        a, b = self.ew(), self.ew(visibility='SHARED')
        c1 = self.ce(is_shared=True)
        other = self.ce(project=self.pb, org=self.org_b, actor=self.bob)
        self.assertEqual((a.ref, b.ref, c1.ref, other.ref), ('EW-001', 'EW-002', 'CE-001', 'CE-001'))
        self.assertEqual({a.visibility, b.visibility, c1.visibility}, {'INTERNAL'})
        with org_context(self.org_a):
            self.assertEqual(set(RecordIndex.objects.filter(project=self.pa).values_list('visibility', flat=True)),
                             {'INTERNAL'})

    def test_totals_are_decimal(self):
        c = self.ce()
        with org_context(self.org_a):
            for kind, amount in (('COST', '100.10'), ('COST', '0.20'), ('REVENUE', '150.00')):
                ChangeEventLine.objects.create(change_event=c, kind=kind, description='x', amount=Decimal(amount))
            c = ChangeEvent.objects.get(pk=c.pk)
            self.assertEqual((c.cost_total, c.revenue_total, c.margin),
                             (Decimal('100.30'), Decimal('150.00'), Decimal('49.70')))


class TransitionTests(ChangeTestBase):
    def detail(self, c):
        return self.url('change_event_detail', self.pa.pk, c.pk)

    def test_change_event_lifecycle_through_the_view(self):
        c = self.ce()
        self.login(self.pat)
        for action, status in (('notify', 'NOTIFIED'), ('quote', 'QUOTED'), ('agree', 'AGREED'),
                               ('implement', 'IMPLEMENTED')):
            self.assertEqual(self.client.post(self.detail(c), {'action': action}).status_code, 302)
            with org_context(self.org_a):
                self.assertEqual(ChangeEvent.objects.get(pk=c.pk).status, status, action)
        with org_context(self.org_a):
            c = ChangeEvent.objects.get(pk=c.pk)
            idx = RecordIndex.objects.get(ref=c.ref, project=self.pa)
            self.assertEqual([e.action for e in AuditEntry.objects.filter(record_idx=idx).order_by('pk')],
                             ['created', 'notify', 'quote', 'agree', 'implement'])
            self.assertFalse(idx.is_open)
        today = timezone.localdate()
        self.assertEqual((c.notified_on, c.quotation_submitted_on, c.reply_received_on), (today, today, today))

    def test_wrong_source_status_is_a_message_not_a_change(self):
        c = self.ce()
        self.login(self.pat)
        r = self.client.post(self.detail(c), {'action': 'implement'}, follow=True)
        self.assertContains(r, 'Cannot mark as implemented')
        with org_context(self.org_a):
            self.assertEqual(ChangeEvent.objects.get(pk=c.pk).status, 'IDENTIFIED')

    def test_reject(self):
        c = self.ce()
        self.login(self.alice)
        self.client.post(self.detail(c), {'action': 'reject'})
        with org_context(self.org_a):
            self.assertEqual(ChangeEvent.objects.get(pk=c.pk).status, 'REJECTED')

    def test_early_warning_close_reopen_and_escalate(self):
        e = self.ew(ball_in_court_user=self.pat)
        url = self.url('early_warning_detail', self.pa.pk, e.pk)
        self.login(self.pat)
        self.client.post(url, {'action': 'close'})
        with org_context(self.org_a):
            self.assertEqual(EarlyWarning.objects.get(pk=e.pk).status, 'CLOSED')
        self.client.post(url, {'action': 'reopen'})
        r = self.client.post(url, {'action': 'escalate'})
        with org_context(self.org_a):
            e = EarlyWarning.objects.get(pk=e.pk)
            c = ChangeEvent.objects.get(project=self.pa)
            self.assertEqual((e.status, c.origin, c.ball_in_court_user), ('ESCALATED', 'EARLY_WARNING', self.pat))
            self.assertEqual([i.ref for i in came_from(c)], [e.ref])
            self.assertEqual([i.ref for i in led_to(e)], [c.ref])
        self.assertRedirects(r, c.get_absolute_url())

    def test_externals_cannot_transition(self):
        c = self.ce()
        for name in self.EXTERNALS:
            self.assertEqual(self.status_for(getattr(self, name), self.detail(c), 'post', {'action': 'notify'}), 403)
        with org_context(self.org_a):
            self.assertEqual(ChangeEvent.objects.get(pk=c.pk).status, 'IDENTIFIED')

    def test_lines_through_the_view(self):
        c = self.ce()
        self.login(self.pat)
        self.client.post(self.detail(c), {'action': 'add_line', 'kind': 'REVENUE', 'description': 'To client',
                                          'amount': '1200.50'})
        self.client.post(self.detail(c), {'action': 'add_line', 'kind': 'COST', 'description': 'Sub',
                                          'amount': '800'})
        with org_context(self.org_a):
            c = ChangeEvent.objects.get(pk=c.pk)
            self.assertEqual((c.cost_total, c.revenue_total), (Decimal('800.00'), Decimal('1200.50')))
            line = c.lines.get(kind='COST')
        self.client.post(self.detail(c), {'action': 'delete_line', 'line': line.pk})
        with org_context(self.org_a):
            self.assertEqual(ChangeEvent.objects.get(pk=c.pk).cost_total, Decimal('0.00'))


class ChainTests(ChangeTestBase):
    def test_create_from_rfi_links_it(self):
        r = self.rfi()
        self.login(self.pat)
        resp = self.client.post(self.url('change_event_create', self.pa.pk), {
            'title': 'Ramps', 'origin': 'RFI', 'source_rfi': r.pk, 'description': 'x',
            'ball_in_court_user': self.pat.pk, 'aware_on': '2026-10-01'})
        self.assertEqual(resp.status_code, 302)
        with org_context(self.org_a):
            c = ChangeEvent.objects.get(title='Ramps')
            self.assertEqual([i.ref for i in came_from(c, 'rfis.rfi')], [r.ref])

    def test_origin_rfi_needs_the_rfi(self):
        self.login(self.pat)
        r = self.client.post(self.url('change_event_create', self.pa.pk), {
            'title': 'Ramps', 'origin': 'RFI', 'description': 'x'})
        self.assertContains(r, 'Pick the RFI this came from')

    def test_ce_becomes_a_draft_variation_linked_to_both(self):
        r = self.rfi()
        c = self.ce(origin='RFI', time_impact_days=3)
        with org_context(self.org_a):
            from ..links import set_source
            set_source(c, r, 'rfis.rfi', actor=self.pat)
            ChangeEventLine.objects.create(change_event=c, kind='REVENUE', description='x', amount=Decimal('5450'))
        self.login(self.pat)
        resp = self.client.post(self.url('change_event_detail', self.pa.pk, c.pk), {'action': 'raise_variation'})
        with org_context(self.org_a):
            vo = Variation.objects.get(project=self.pa)
            self.assertRedirects(resp, vo.get_absolute_url())
            self.assertEqual((vo.status, vo.visibility, vo.value, vo.time_impact_days, vo.rfi_id, vo.created_by),
                             ('DRAFT', 'INTERNAL', Decimal('5450.00'), 3, r.pk, self.pat))
            self.assertEqual([i.ref for i in came_from(vo, 'changes.changeevent')], [c.ref])
            self.assertEqual([i.ref for i in came_from(vo, 'rfis.rfi')], [r.ref])
            self.assertEqual([i.ref for i in led_to(c, 'variations.variation')], [vo.ref])
        # A second press doesn't make a second variation
        self.client.post(self.url('change_event_detail', self.pa.pk, c.pk), {'action': 'raise_variation'})
        with org_context(self.org_a):
            self.assertEqual(Variation.objects.filter(project=self.pa).count(), 1)
        # The variation page shows where it came from, to staff
        self.assertContains(self.client.get(vo.get_absolute_url()), c.ref)

    def test_client_never_sees_the_change_event_behind_a_variation(self):
        c = self.ce(title='Internal pricing note')
        self.login(self.pat)
        self.client.post(self.url('change_event_detail', self.pa.pk, c.pk), {'action': 'raise_variation'})
        with org_context(self.org_a):
            vo = Variation.objects.get(project=self.pa)
            apply_transition(vo, 'submit', self.pat)
        self.client.logout()
        self.login(self.carla)
        r = self.client.get(vo.get_absolute_url())
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, c.ref)
        self.assertNotContains(r, 'Commercial')

    def test_raising_twice_returns_the_first_variation(self):
        # Regression: two presses (or two tabs) each made a VO; the CE row is now locked and re-checked
        from ..views import raise_variation
        c = self.ce()
        with org_context(self.org_a):
            first, created = raise_variation(c, self.pat)
            again, created_again = raise_variation(ChangeEvent.objects.get(pk=c.pk), self.pat)
            self.assertEqual((created, created_again, again.pk), (True, False, first.pk))
            self.assertEqual(Variation.objects.filter(project=self.pa).count(), 1)

    def test_sub_variation_decision_drafts_an_audited_linked_variation(self):
        # Regression: the client variation made from an approved sub variation had no audit actor and no link
        c = self.ce()
        with org_context(self.org_a):
            sv = SubVariation.objects.create(project=self.pa, subcontractor=self.subco, title='Extra shelving',
                                             description='x', value=Decimal('1200'), change_event=c)
        self.login(self.pat)
        r = self.client.post(self.url('subvariation_detail', self.pa.pk, sv.pk), {
            'action': 'decide', 'decision': 'APPROVED', 'approved_value': '1100', 'raise_client_variation': 'on'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            vo = SubVariation.objects.get(pk=sv.pk).client_variation
            idx = RecordIndex.objects.get(ref=vo.ref, project=self.pa)
            created = AuditEntry.objects.get(record_idx=idx, action='created')
            self.assertEqual((created.actor, vo.created_by, vo.value), (self.pat, self.pat, Decimal('1100.00')))
            self.assertEqual([i.ref for i in came_from(vo, 'changes.changeevent')], [c.ref])

    def test_sub_variation_tied_and_untied(self):
        c = self.ce()
        with org_context(self.org_a):
            sv = SubVariation.objects.create(project=self.pa, subcontractor=self.subco, title='Re-veneer',
                                             description='x', value=Decimal('2350'))
        self.login(self.pat)
        url = self.url('change_event_detail', self.pa.pk, c.pk)
        self.client.post(url, {'action': 'link_sub_variation', 'sub_variation': sv.pk})
        with org_context(self.org_a):
            self.assertEqual(SubVariation.objects.get(pk=sv.pk).change_event_id, c.pk)
        self.assertContains(self.client.get(url), sv.reference)
        self.client.post(url, {'action': 'unlink_sub_variation', 'sv': sv.pk})
        with org_context(self.org_a):
            self.assertIsNone(SubVariation.objects.get(pk=sv.pk).change_event_id)


class AccessTests(ChangeTestBase):
    def urls(self):
        e, c = self.ew(), self.ce()
        return [self.url('change_register', self.pa.pk), self.url('early_warning_create', self.pa.pk),
                self.url('early_warning_detail', self.pa.pk, e.pk), self.url('early_warning_edit', self.pa.pk, e.pk),
                self.url('change_event_create', self.pa.pk), self.url('change_event_detail', self.pa.pk, c.pk),
                self.url('change_event_edit', self.pa.pk, c.pk), self.url('contract_detail', self.pa.pk),
                self.url('company_change_register')]

    def test_roles(self):
        expected = {'alice': 200, 'adam': 200, 'pat': 200, 'carla': 403, 'connie': 403, 'sid': 403, 'bea': 403,
                    'nora': 403}
        for url in self.urls():
            for name, code in expected.items():
                with self.subTest(url=url, who=name):
                    self.assertEqual(self.status_for(getattr(self, name), url), code)

    def test_estimating_staff_and_other_companies(self):
        for url in self.urls()[:-1]:  # project pages
            self.assertEqual(self.status_for(self.eve, url), 404)
            self.assertEqual(self.status_for(self.bob, url), 404)
        self.login(self.bob)
        self.assertNotContains(self.client.get(self.url('company_change_register', org=self.org_b)), 'Riser approval')

    def test_externals_never_see_them_in_the_record_index(self):
        e, c = self.ew(ball_in_court_user=self.carla), self.ce(ball_in_court_user=self.carla)
        with org_context(self.org_a):
            for who in (self.carla, self.connie, self.sid):
                refs = set(visible_index(self.request_for(who)).values_list('ref', flat=True))
                self.assertFalse(refs & {e.ref, c.ref}, who)
            self.assertEqual(list(needs_me(self.request_for(self.carla))), [])
            self.assertEqual({e.ref, c.ref} & set(visible_index(self.request_for(self.pat)).values_list('ref', flat=True)),
                             {e.ref, c.ref})

    def test_external_pages_do_not_mention_them(self):
        self.ew(title='Landlord riser approval')
        self.ce(title='Threshold ramps')
        self.login(self.carla)
        for name in ('project_detail', 'rfi_list', 'variation_list'):
            r = self.client.get(self.url(name, self.pa.pk))
            self.assertNotContains(r, 'Landlord riser approval')
            self.assertNotContains(r, 'Threshold ramps')
            self.assertNotContains(r, 'Change register')
        self.assertNotContains(self.client.get(self.url('rfi_register')), 'Threshold ramps')

    def test_client_still_sees_shared_rfis_with_a_bidder_on_the_project(self):
        # Regression: portal_projects() used to drop any project with a bidder on it for every external
        r = self.rfi(title='Shared question', visibility='SHARED')
        with org_context(self.org_a):
            self.assertIn(r.ref, set(visible_index(self.request_for(self.carla)).values_list('ref', flat=True)))
            self.assertFalse(visible_index(self.request_for(self.bea)).exists())

    def test_tenancy(self):
        c = self.ce()
        with org_context(self.org_b):
            self.assertFalse(ChangeEvent.objects.filter(pk=c.pk).exists())
            self.assertFalse(EarlyWarning.objects.exists())
        for model in (ChangeEvent, EarlyWarning, ChangeEventLine):
            with self.subTest(model.__name__), self.assertRaises(TenantContextMissing):
                model.objects.count()
        self.login(self.bob)
        self.assertEqual(self.client.get(self.url('change_event_detail', self.pa.pk, c.pk, org=self.org_b)).status_code,
                         404)


class RegisterTests(ChangeTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            MainContract.objects.create(project=self.pa, form='NEC4_ECC', nec_main_option='A')
        today = timezone.localdate()
        self.late = self.ce(title='Late one', aware_on=today - timedelta(weeks=8, days=1), ball_in_court_user=self.pat)
        self.fresh = self.ce(title='Fresh one', aware_on=today, origin='SITE_ISSUE')
        self.done = self.ce(title='Done one')
        with org_context(self.org_a):
            apply_transition(self.done, 'reject', self.pat)
        self.warning = self.ew(title='A warning')

    def get(self, query='', url=None):
        self.login(self.pat)
        return self.client.get((url or self.url('change_register', self.pa.pk)) + query)

    def test_live_by_default(self):
        r = self.get()
        self.assertContains(r, 'Late one')
        self.assertContains(r, 'A warning')
        self.assertNotContains(r, 'Done one')
        self.assertContains(r, 'Advisory')
        self.assertContains(r, 'day late')

    def test_filters(self):
        self.assertContains(self.get('?status=REJECTED'), 'Done one')
        r = self.get('?origin=SITE_ISSUE')
        self.assertContains(r, 'Fresh one')
        self.assertNotContains(r, 'Late one')
        self.assertNotContains(r, 'A warning')
        r = self.get('?attention=1')
        self.assertContains(r, 'Late one')
        self.assertNotContains(r, 'Fresh one')  # 56 days left
        r = self.get('?show=ew')
        self.assertContains(r, 'A warning')
        self.assertNotContains(r, 'Late one')
        r = self.get('?mine=1')
        self.assertContains(r, 'Late one')
        self.assertNotContains(r, 'Fresh one')

    def test_empty_states_explain(self):
        r = self.get('?status=IMPLEMENTED')
        self.assertContains(r, 'Nothing matches these filters')

    def test_company_register(self):
        with org_context(self.org_a):
            from projects.models import Project
            other = Project.objects.create(name='Second job', stage=Project.STAGE_LIVE)
        self.ce(project=other, title='Other job event')
        url = self.url('company_change_register')
        r = self.get(url=url)
        self.assertContains(r, 'Late one')
        self.assertContains(r, 'Other job event')
        r = self.get(f'?project={other.pk}', url=url)
        self.assertContains(r, 'Other job event')
        self.assertNotContains(r, 'Late one')

    def test_jct_prompt_on_the_detail_page(self):
        with org_context(self.org_a):
            MainContract.objects.filter(project=self.pa).update(form='JCT_IC', nec_main_option='')
        c = self.ce(title='Delaying event', time_impact_days=4)
        r = self.get(url=self.url('change_event_detail', self.pa.pk, c.pk))
        self.assertContains(r, 'Notice of delay')
        self.assertContains(r, 'Contract Administrator')
        self.assertNotContains(r, 'compensation event')

    def test_nec_clocks_on_the_detail_page(self):
        r = self.get(url=self.url('change_event_detail', self.pa.pk, self.late.pk))
        self.assertContains(r, 'Notify the compensation event')
        self.assertContains(r, '8-week time bar')
        self.assertContains(r, 'NEC4 61.3')


class ActionItemsTests(ChangeTestBase):
    def test_items_for_owner_manager_and_externals(self):
        today = timezone.localdate()
        with org_context(self.org_a):
            MainContract.objects.create(project=self.pa, form='NEC4_ECC', nec_main_option='A')
        late = self.ce(title='Late', aware_on=today - timedelta(weeks=8, days=2), ball_in_court_user=self.alice)
        unowned = self.ce(title='Nobody owns it')  # falls to the project manager (pat)
        self.ew(title='Meeting soon', ball_in_court_user=self.alice, meeting_date=today + timedelta(days=2))
        with org_context(self.org_a):
            mine = items(self.request_for(self.alice))
            self.assertEqual([i['ref'] for i in mine], [late.ref, 'EW-001'])
            self.assertEqual(mine[0]['tone'], 'bad')
            self.assertIn('8-week time bar', mine[0]['reason'])
            self.assertEqual(set(mine[0]) >= {'kind', 'ref', 'title', 'url', 'project', 'due', 'tone', 'reason'}, True)
            self.assertEqual([i['ref'] for i in items(self.request_for(self.pat))], [unowned.ref])
            for who in (self.carla, self.sid, self.connie):
                self.assertEqual(items(self.request_for(who)), [])
        with org_context(self.org_b):
            self.assertEqual(items(self.request_for(self.bob, org=self.org_b)), [])
