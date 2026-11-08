"""Drafting a handover pack's clarifications: what is sent, how drafts are saved, approved and kept out of the pack."""
import io

import openpyxl

from ai.models import AIRun, AIUsage
from handoverpack import clarifications, pack as packs, exports, services
from handoverpack.models import HandoverPack, PackClarification
from handoverpack.tests.test_views import upload_file
from jobs.models import Job
from tenancy.context import org_context

from .base import Base, fake_ai, message, run_worker


def reply(*items):
    return message({'clarifications': [{'trade': t, 'text': x, 'basis_line_refs': refs} for t, x, refs in items]})


class Setup(Base):
    def setUp(self):
        super().setUp()
        self.switch_on()
        with org_context(self.org_a):
            services.upload(self.bid, self.eve, upload_file())
            self.pack = HandoverPack.objects.get(project=self.bid)
            self.pack.site_rules = 'Deliveries via the rear bay. Call the building manager on 07700 900123 or ' \
                                   'manager@landlord.example to book.'
            self.pack.save()

    def draft(self, user=None, *replies):
        self.login(user or self.eve)
        with fake_ai(*replies) as client:
            r = self.client.post(self.url('ai_draft_clarifications', self.bid.pk))
            run_worker()
        return r, client


class InputTests(Setup):
    def test_only_what_the_feature_needs_is_sent(self):
        with org_context(self.org_a):
            text, refs = clarifications.build_input(self.pack)
            wb = self.pack.workbook.result
        self.assertIn('Metal stud partition', text)
        self.assertIn('186', text)                       # quantities go
        self.assertIn('Provisional sum', text)
        self.assertIn('£5,000', text)                    # the provisional sum's figure goes
        self.assertIn('Signage', text)                   # an excluded trade is named
        self.assertIn('Deliveries via the rear bay', text)
        self.assertNotIn('07700', text)                  # personal details are taken out
        self.assertNotIn('manager@landlord', text)
        for supplier in ('Brightwall', 'Fernside', 'Voltline', 'Airflow'):
            self.assertNotIn(supplier, text)             # no supplier names
        self.assertNotIn(str(wb['totals']['cost']), text)  # no cost or margin
        self.assertNotIn('margin', text.lower())
        self.assertNotIn('Tom Reilly', text)             # the workbook's estimator
        self.assertTrue(refs and all(k.startswith('L') for k in refs))


class DraftTests(Setup):
    def test_drafts_land_unapproved_and_stay_out_of_the_pack_and_export(self):
        r, client = self.draft(None, reply(
            ('Partitions Ceilings', 'Allowed 186 m2 of metal stud partition to HL-A-201.', ['L16', 'L999', 7]),
            ('Electrical', '<script>alert(1)</script> Data cabling by the client IT contractor.', []),
            ('', '', ['L1'])))
        self.assertRedirects(r, self.url('hpack_clarifications', self.bid.pk))
        self.assertEqual(len(client.requests), 1)
        with org_context(self.org_a):
            drafts = list(PackClarification.objects.filter(pack=self.pack, source=PackClarification.FROM_DRAFT))
            run = AIRun.objects.get()
            self.assertEqual(AIUsage.objects.get().run, run)
        self.assertEqual(run.status, AIRun.DONE)
        self.assertEqual(len(drafts), 2)                 # the empty one is dropped
        self.assertTrue(all(not d.approved for d in drafts))
        self.assertRegex(drafts[0].basis, r'^Partitions Ceilings row \d+$')  # unknown refs dropped
        # The page shows them marked, escaped
        page = self.client.get(self.url('hpack_clarifications', self.bid.pk))
        self.assertContains(page, 'Not approved')
        self.assertContains(page, '&lt;script&gt;')
        self.assertNotContains(page, '<script>alert(1)')
        # Not in the pack, its export or anything built from visible_clarifications
        with org_context(self.org_a):
            ctx = packs.build(self.bid, self.pack, self.pack.workbook)
            self.assertFalse([c for c in ctx['clarifications'] if c.source == PackClarification.FROM_DRAFT])
            book = openpyxl.load_workbook(io.BytesIO(exports.build(ctx, 'Alpha')))
        cells = ' '.join(str(c.value) for ws in book for row in ws.iter_rows() for c in row if c.value)
        self.assertNotIn('Allowed 186 m2', cells)
        export = self.client.get(self.url('hpack_pack', self.bid.pk) + '?export=xlsx')
        self.assertEqual(export.status_code, 200)

    def test_approve_puts_it_in_the_pack_and_redraft_keeps_touched_rows(self):
        self.draft(None, reply(('Doors', 'Allowed 14 FD30S doorsets.', []), ('Doors', 'Untouched draft.', []),
                               ('Doors', 'To be edited.', [])))
        with org_context(self.org_a):
            approve, untouched, edit = PackClarification.objects.filter(source='DRAFT').order_by('order')
        self.client.post(self.url('hpack_clarification', self.bid.pk, approve.pk), {'approve_only': '1'})
        self.client.post(self.url('hpack_clarification', self.bid.pk, edit.pk),
                         {'trade': 'Doors', 'text': 'Edited by a person.', 'status': 'Open'})
        with org_context(self.org_a):
            approve.refresh_from_db()
            edit.refresh_from_db()
            self.assertTrue(approve.approved)
            self.assertEqual(approve.approved_by, self.eve)
            self.assertFalse(edit.approved)
            self.assertTrue(edit.edited)
            ctx = packs.build(self.bid, self.pack, self.pack.workbook)
            self.assertIn(approve, ctx['clarifications'])
            self.assertNotIn(edit, ctx['clarifications'])
        self.draft(None, reply(('Doors', 'Allowed 14 FD30S doorsets.', []), ('Doors', 'A new one.', [])))
        with org_context(self.org_a):
            texts = set(PackClarification.objects.filter(source='DRAFT').values_list('text', flat=True))
        self.assertEqual(texts, {'Allowed 14 FD30S doorsets.', 'Edited by a person.', 'A new one.'})

    def test_save_and_approve_from_the_edit_page(self):
        self.draft(None, reply(('Doors', 'Draft.', [])))
        with org_context(self.org_a):
            c = PackClarification.objects.get(source='DRAFT')
        self.client.post(self.url('hpack_clarification', self.bid.pk, c.pk),
                         {'trade': 'Doors', 'text': 'Checked.', 'status': 'Open', 'approve': '1'})
        with org_context(self.org_a):
            c.refresh_from_db()
        self.assertTrue(c.approved and c.edited)

    def test_long_and_many_drafts_are_capped(self):
        items = [('T' * 200, f'{i} ' + 'x' * 5000, []) for i in range(80)]
        self.draft(None, reply(*items))
        with org_context(self.org_a):
            rows = list(PackClarification.objects.filter(source='DRAFT'))
        self.assertEqual(len(rows), clarifications.MAX_DRAFTS)
        self.assertTrue(all(len(r.text) <= clarifications.MAX_TEXT and len(r.trade) <= 80 for r in rows))

    def test_unavailable_says_why_and_queues_nothing(self):
        self.switch_on(enabled=False)
        r, client = self.draft(None)
        self.assertEqual(client.requests, [])
        self.assertFalse(Job.objects.filter(name='ai.run').exists())
        page = self.client.get(self.url('hpack_clarifications', self.bid.pk))
        self.assertContains(page, 'switched off for your company')

    def test_a_run_picked_up_twice_is_not_paid_for_twice(self):
        with org_context(self.org_a):
            run = AIRun.objects.create(project=self.bid, feature='clarifications', requested_by=self.eve,
                                       status=AIRun.RUNNING)
        from ai.tasks import run_job
        with fake_ai(reply(('Doors', 'x', []))) as client, org_context(self.org_a):
            run_job({'run_id': run.pk})
            run.refresh_from_db()
        self.assertEqual(client.requests, [])
        self.assertEqual(run.status, AIRun.FAILED)
        self.assertIn('interrupted', run.message)
