"""Regression tests for the RFI drafting review (R1 to R5). No network."""
import importlib

from django.utils import timezone

from projects.models import Document
from rfis.models import RFI
from tenancy.context import org_context
from tenancy.models import Notification

from .test_rfis import Setup, reply, rfi


class Drafted(Setup):
    def setUp(self):
        super().setUp()
        self.draft(reply(rfi(subject='Ceiling grid conflict SECRET-DRAFT')))
        with org_context(self.org_a):
            self.d = RFI.objects.get(project=self.bid, ai_drafted=True)


class R1ViewerHidesDrafts(Drafted):
    def test_a_shared_draft_pinned_on_a_shared_drawing_is_invisible_to_a_client(self):
        with org_context(self.org_a):
            self.d.is_shared = True
            self.d.save(actor=self.eve)
            doc = self.spec.document
            doc.visibility = Document.VIS_SHARED
            doc.save()
            self.spec.shared_at = timezone.now()
            self.spec.save()
            from viewer.models import DrawingPin
            DrawingPin.objects.create(project=self.bid, document=doc, revision=self.spec, page=1, x='0.5', y='0.5',
                                      rfi=self.d, created_by=self.eve)
        from viewer.access import visible_pins, visible_rfis
        req = self.request_for(self.carla)
        with org_context(self.org_a):
            self.assertNotIn(self.d, list(visible_rfis(req, self.bid)))
            self.assertEqual(list(visible_pins(req, self.bid)), [])
        self.login(self.carla)
        page = self.client.get(self.url('drawing_viewer', self.bid.pk, self.spec.document.pk, self.spec.pk))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'SECRET-DRAFT')
        self.assertNotContains(page, self.url('rfi_detail', self.bid.pk, self.d.pk))
        # Staff still see it
        self.login(self.eve)
        page = self.client.get(self.url('drawing_viewer', self.bid.pk, self.spec.document.pk, self.spec.pk))
        self.assertContains(page, 'SECRET-DRAFT')

    def test_one_helper_decides(self):
        with org_context(self.org_a):
            self.d.is_shared = True
            self.d.save(actor=self.eve)
            qs = RFI.objects.filter(project=self.bid)
            self.assertNotIn(self.d, RFI.for_externals(qs))
            self.assertNotIn(self.d, RFI.visible_to(self.request_for(self.carla), qs))
            self.assertIn(self.d, RFI.visible_to(self.request_for(self.eve), qs))


class R2Notifications(Drafted):
    def test_issuing_an_internal_rfi_warns_instead_of_telling_an_external(self):
        with org_context(self.org_a):
            self.d.ball_in_court_user = self.carla
            self.d.save(actor=self.eve)
        self.login(self.eve)
        r = self.client.post(self.url('rfi_detail', self.bid.pk, self.d.pk), {'action': 'issue'}, follow=True)
        self.assertContains(r, 'can&#x27;t see this RFI while it is internal')
        with org_context(self.org_a):
            self.assertFalse(Notification.objects.filter(user=self.carla).exists())

    def test_shared_or_staff_ball_in_court_is_told(self):
        with org_context(self.org_a):
            self.d.ball_in_court_user = self.adam
            self.d.save(actor=self.eve)
        self.login(self.eve)
        self.client.post(self.url('rfi_detail', self.bid.pk, self.d.pk), {'action': 'issue'})
        with org_context(self.org_a):
            self.assertTrue(Notification.objects.filter(user=self.adam).exists())
        # Passing an internal RFI to the client: warned, not notified; once shared, notified
        self.client.post(self.url('rfi_detail', self.bid.pk, self.d.pk),
                         {'action': 'reassign', 'ball_in_court_user': self.carla.pk})
        with org_context(self.org_a):
            self.assertFalse(Notification.objects.filter(user=self.carla).exists())
            self.d.refresh_from_db()
            self.d.is_shared = True
            self.d.ball_in_court_user = self.adam
            self.d.save(actor=self.eve)
        self.client.post(self.url('rfi_detail', self.bid.pk, self.d.pk),
                         {'action': 'reassign', 'ball_in_court_user': self.carla.pk})
        with org_context(self.org_a):
            self.assertTrue(Notification.objects.filter(user=self.carla).exists())


class R3NumberedOnIssue(Setup):
    def test_drafts_take_no_number_and_issuing_takes_the_next(self):
        self.draft(reply(*[rfi(subject=f'Q{i}') for i in range(5)]))
        with org_context(self.org_a):
            drafts = list(RFI.objects.filter(project=self.bid, ai_drafted=True).order_by('pk'))
            self.assertEqual({(d.number, d.ref, d.reference) for d in drafts}, {(None, '', 'Draft')})
            manual = RFI(project=self.bid, title='Hand-raised', question='?')
            manual.save(actor=self.eve)
        self.assertEqual(manual.reference, 'RFI-001')
        self.login(self.eve)
        self.client.post(self.url('rfi_detail', self.bid.pk, drafts[3].pk), {'action': 'issue'})
        self.client.post(self.url('rfi_detail', self.bid.pk, drafts[0].pk), {'action': 'void'})  # never numbered
        with org_context(self.org_a):
            drafts[3].refresh_from_db()
            drafts[0].refresh_from_db()
            nxt = RFI(project=self.bid, title='Next', question='?')
            nxt.save(actor=self.eve)
        self.assertEqual((drafts[3].number, drafts[3].reference), (2, 'RFI-002'))
        self.assertIsNone(drafts[0].number)
        self.assertEqual(nxt.reference, 'RFI-003')
        page = self.client.get(self.url('rfi_list', self.bid.pk) + '?status=DRAFT')
        self.assertContains(page, 'Draft')


class R4PickersOfferIssuedOnly(Drafted):
    def test_variation_change_event_and_diary_pickers(self):
        from changes.forms import ChangeEventForm
        from diary.forms import InstructionForm
        from variations.forms import VariationForm
        with org_context(self.org_a):
            issued = RFI(project=self.bid, title='Issued one', question='?')
            issued.save(actor=self.eve)
            forms = [VariationForm(project=self.bid).fields['rfi'],
                     ChangeEventForm(project=self.bid).fields['source_rfi'],
                     InstructionForm(project=self.bid).fields['rfi']]
            for field in forms:
                pks = set(field.queryset.values_list('pk', flat=True))
                self.assertIn(issued.pk, pks)
                self.assertNotIn(self.d.pk, pks)


class R5MigrationRollback(Drafted):
    def test_going_back_makes_drafts_internal_and_numbered(self):
        from django.apps import apps
        with org_context(self.org_a):
            self.d.is_shared = True
            self.d.save(actor=self.eve)
            issued = RFI(project=self.bid, title='Issued', question='?')
            issued.save(actor=self.eve)
        mig = importlib.import_module('rfis.migrations.0006_ai_drafted')
        mig.before_going_back(apps, None)
        with org_context(self.org_a):
            self.d.refresh_from_db()
        self.assertEqual((self.d.visibility, self.d.number, self.d.ref), (RFI.VIS_INTERNAL, 2, 'RFI-002'))
        self.assertEqual(mig.Migration.operations[-1].reverse_code, mig.before_going_back)
