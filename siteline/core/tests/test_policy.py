"""
The housekeeping rules (recommended defaults pending the product owner, see ADR 0001):
retention, legal hold, who deletes, restores and purges, and GDPR redaction.
"""
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied
from django.test import override_settings
from django.utils import timezone

from core.exceptions import PurgeRefused
from core.models import REDACTED, AuditEntry, RecordIndex, RetentionPolicy, add_years, redact
from core.workflow import apply_transition
from rfis.models import RFI
from tenancy.context import org_context, unscoped
from tenancy.models import Membership

from .base import RecordTestBase, make_user


class PolicyBase(RecordTestBase):
    def setUp(self):
        super().setUp()
        self.adam = make_user('adam@a.test', 'Adam Admin')
        self.sam = make_user('sam@a.test', 'Sam Staff')
        Membership.objects.create(organization=self.org_a, user=self.adam, role=Membership.ROLE_ADMIN)
        Membership.objects.create(organization=self.org_a, user=self.sam, role=Membership.ROLE_MEMBER)


class RetentionTests(PolicyBase):
    def complete(self, day):
        with org_context(self.org_a):
            self.pa.completion_date = day
            self.pa.save()

    def test_default_is_project_completion_plus_twelve_years(self):
        self.complete(date(2020, 3, 1))
        r = self.rfi()
        self.assertEqual(r.effective_retain_until(), date(2032, 3, 1))

    def test_organisation_setting_and_settings_default(self):
        self.complete(date(2020, 3, 1))
        r = self.rfi()
        with override_settings(RECORD_RETENTION_YEARS=6):
            self.assertEqual(r.effective_retain_until(), date(2026, 3, 1))
        with org_context(self.org_a):
            RetentionPolicy.objects.create(retention_years=15)
        self.assertEqual(r.effective_retain_until(), date(2035, 3, 1))
        self.assertEqual(RetentionPolicy.years_for(self.org_b.pk), 12)  # another company keeps the default

    def test_leap_day(self):
        self.assertEqual(add_years(date(2024, 2, 29), 1), date(2025, 2, 28))

    def test_no_completion_date_means_no_purge(self):
        r = self.rfi()
        with org_context(self.org_a):
            r.soft_delete(self.alice)
            with self.assertRaisesMessage(PurgeRefused, 'no completion date'):
                r.purge(self.alice)

    def test_purge_after_the_default_period(self):
        self.complete(timezone.localdate() - timedelta(days=366 * 12 + 3))
        r = self.rfi()
        with org_context(self.org_a):
            r.soft_delete(self.alice)
            r.purge(self.alice)
        self.assertFalse(RFI.all_objects.filter(title='Which veneer?').exists())


class PermissionTests(PolicyBase):
    def test_only_owners_and_admins_set_or_clear_legal_hold(self):
        r = self.rfi()
        with org_context(self.org_a):
            for who in (self.sam, None):
                r = RFI.objects.get(pk=r.pk)
                r.legal_hold = True
                with self.assertRaises(PermissionDenied):
                    r.save(actor=who)
            r = RFI.objects.get(pk=r.pk)
            r.legal_hold = True
            r.save(actor=self.adam)
            r.legal_hold = False
            with self.assertRaises(PermissionDenied):
                r.save(actor=self.sam)
            r = RFI.objects.get(pk=r.pk)
            r.legal_hold = False
            r.save(actor=self.alice)
            self.assertFalse(RFI.objects.get(pk=r.pk).legal_hold)

    def test_staff_soft_delete_their_own_drafts_only(self):
        own_draft = self.rfi(actor=self.sam, status=RFI.STATUS_DRAFT)
        own_open = self.rfi(actor=self.sam)
        others_draft = self.rfi(actor=self.alice, status=RFI.STATUS_DRAFT)
        with org_context(self.org_a):
            own_draft.soft_delete(self.sam)
            for r in (own_open, others_draft):
                with self.assertRaises(PermissionDenied):
                    r.soft_delete(self.sam)
            with self.assertRaises(PermissionDenied):
                own_draft.restore(self.sam)  # restoring is for admins
            own_draft.restore(self.adam)
            own_open.soft_delete(self.adam)  # admins delete anything
            with self.assertRaises(PermissionDenied):
                own_open.soft_delete(self.carla)  # externals never

    def test_only_owners_purge(self):
        r = self.rfi(retain_until=timezone.localdate())
        with org_context(self.org_a):
            r.soft_delete(self.adam)
            with self.assertRaises(PermissionDenied):
                r.purge(self.adam)
            r.purge(self.alice)


class AnsweringTests(PolicyBase):
    def test_answering_through_the_page_passes_the_ball_back(self):
        r = self.rfi(actor=self.sam, ball_in_court_user=self.carla)
        self.login(self.carla)
        self.client.post(self.url('rfi_detail', self.org_a, self.pa.pk, r.pk), {'action': 'respond', 'response': 'Oak'})
        with org_context(self.org_a):
            idx = RecordIndex.objects.get(object_id=r.pk)
            self.assertEqual(RFI.objects.get(pk=r.pk).ball_in_court_user, self.sam)
        self.assertEqual((idx.ball_in_court_user, idx.is_open), (self.sam, True))


class RedactionTests(PolicyBase):
    def test_redacting_a_record_scrubs_its_trail_and_title(self):
        r = self.rfi(title='Call Jo Bloggs on 07700 900123', question='?')
        with org_context(self.org_a):
            r.title = 'Ask Jo Bloggs'
            r.save(actor=self.alice)
            changed = redact(r, self.adam, 'Erasure request 2026-11')
            idx = RecordIndex.objects.get(object_id=r.pk)
            trail = list(AuditEntry.objects.filter(record_idx=idx))
        self.assertEqual(changed, 2)
        self.assertEqual((r.title, idx.title), (REDACTED, REDACTED))
        self.assertEqual(trail[1].changes['title'], [REDACTED, REDACTED])
        self.assertNotIn('Bloggs', str([e.changes for e in trail]))
        self.assertEqual((trail[-1].action, trail[-1].note, trail[-1].changes), ('redacted', 'Erasure request 2026-11', {}))
        with org_context(self.org_a):
            r.save(actor=self.alice)  # the record carries on, and the index doesn't get the old title back
            self.assertEqual(RecordIndex.objects.get(object_id=r.pk).title, REDACTED)

    def test_redacting_a_user_scrubs_their_name_actor_and_references(self):
        dave = make_user('dave@a.test', 'Dave Davies')
        Membership.objects.create(organization=self.org_a, user=dave, role=Membership.ROLE_EXTERNAL)
        mine = self.rfi(actor=dave, title='Dave Davies site access')
        theirs = self.rfi(ball_in_court_user=dave)
        other = self.rfi(title='Unrelated')
        with org_context(self.org_a):
            apply_transition(theirs, 'respond', dave, response='Done, Dave Davies')
            redact(dave, self.adam, 'Erasure request')
            dave_entries = AuditEntry.objects.filter(actor_name=REDACTED)
            self.assertFalse(AuditEntry.objects.filter(actor=dave).exists())
            self.assertEqual(dave_entries.count(), 2)  # raised one, answered another
            dump = str(list(AuditEntry.objects.values_list('changes', 'actor_name', 'note')))
            self.assertNotIn('Dave', dump)
            self.assertNotIn(f"[None, {dave.pk}]", dump)
            self.assertEqual(RecordIndex.objects.get(object_id=mine.pk).title, f'{REDACTED} site access')
            self.assertEqual(RFI.objects.get(pk=mine.pk).title, f'{REDACTED} site access')
            self.assertEqual(RecordIndex.objects.get(object_id=other.pk).title, 'Unrelated')
            self.assertEqual(AuditEntry.objects.filter(action='redacted').count(), 2)
        dave.delete()

    def test_only_owners_admins_and_superusers_redact(self):
        r = self.rfi()
        with org_context(self.org_a):
            with self.assertRaises(PermissionDenied):
                redact(r, self.sam, 'no')
            with self.assertRaises(ValueError):
                redact(r, self.adam, '')
        root = make_user('root@platform.test', 'Root')
        root.is_superuser = True
        root.save()
        with unscoped():
            redact(self.alice, root, 'Platform erasure')  # every organisation, from the platform
        self.assertFalse(AuditEntry.all_objects.filter(actor=self.alice).exists())

    def test_redaction_is_the_only_way_round_append_only(self):
        r = self.rfi()
        with org_context(self.org_a):
            with self.assertRaises(PermissionError):
                AuditEntry.objects.filter(record_idx__object_id=r.pk).update(changes={})
