"""RecordBase on its first adopter (RFIs): numbering, index, audit, soft delete, retention, locking."""
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.db.models.query import QuerySet

from core import models as core_models
from django.utils import timezone

from core.exceptions import PurgeRefused, RecordConflict
from core.models import AuditEntry, NumberSequence, RecordIndex
from core.registry import register_record_type
from projects.models import Issue
from rfis.forms import RFIForm
from rfis.models import RFI
from tenancy.context import org_context

from .base import RecordTestBase


class NumberingTests(RecordTestBase):
    def test_numbers_and_refs_run_per_project(self):
        a1, a2 = self.rfi(), self.rfi()
        other = self.rfi(project=self.pa2)
        self.assertEqual((a1.number, a1.ref, a2.ref), (1, 'RFI-001', 'RFI-002'))
        self.assertEqual(other.ref, 'RFI-001')
        with org_context(self.org_a):
            seqs = dict(NumberSequence.objects.values_list('project_id', 'next_value'))
        self.assertEqual(seqs, {self.pa.pk: 3, self.pa2.pk: 2})

    def test_each_record_type_has_its_own_sequence_on_a_project(self):
        with org_context(self.org_a):
            NumberSequence.objects.create(project=self.pa, key='variations.variation', next_value=40)
        self.assertEqual(self.rfi().ref, 'RFI-001')
        with org_context(self.org_a):
            self.assertEqual(NumberSequence.objects.get(project=self.pa, key='variations.variation').next_value, 40)

    def test_records_built_together_and_saved_in_turn_get_distinct_numbers(self):
        with org_context(self.org_a):
            first = RFI(project=self.pa, title='One', question='?')
            second = RFI(project=self.pa, title='Two', question='?')  # both built before either saves
            second.save(actor=self.alice)
            first.save(actor=self.alice)
        self.assertEqual((second.number, first.number), (1, 2))

    def test_sequence_starts_after_numbers_already_used(self):
        """Rows numbered before the sequence existed (or a lost sequence row) are never reused."""
        self.rfi(), self.rfi(), self.rfi()
        with org_context(self.org_a):
            NumberSequence.objects.all().delete()
        self.assertEqual(self.rfi().ref, 'RFI-004')

    def test_a_read_from_before_the_sequence_existed_finds_it_under_the_project_lock(self):
        """Our first look missed the row another request just made: the locked re-read finds it."""
        with org_context(self.org_a):
            NumberSequence.objects.create(project=self.pa, key='rfis.rfi', next_value=5)  # the other request's row
        real_exists, raced = QuerySet.exists, []

        def exists(qs):
            if qs.model is NumberSequence and not raced:
                raced.append(True)
                return False  # as if read before the other request committed
            return real_exists(qs)

        with mock.patch.object(QuerySet, 'exists', autospec=True, side_effect=exists):
            r = self.rfi()
        self.assertEqual((raced, r.ref), ([True], 'RFI-005'))
        with org_context(self.org_a):
            self.assertEqual(NumberSequence.objects.get(project=self.pa).next_value, 6)

    def test_losing_the_race_to_create_the_sequence_uses_the_winners_row(self):
        """Both requests found no row even under the lock (another database's isolation): the unique constraint decides."""
        with org_context(self.org_a):
            NumberSequence.objects.create(project=self.pa, key='rfis.rfi', next_value=5)
        real, raced = core_models._locked_first, []

        def locked_first(qs):
            if not raced:
                raced.append(True)
                return None
            return real(qs)

        with mock.patch.object(QuerySet, 'exists', autospec=True, return_value=False), \
                mock.patch.object(core_models, '_locked_first', side_effect=locked_first):
            r = self.rfi()
        self.assertEqual(r.ref, 'RFI-005')

    def test_a_failed_save_gives_its_number_back(self):
        self.rfi()
        with mock.patch('core.models.index_record', side_effect=RuntimeError('index down')):
            with self.assertRaises(RuntimeError):
                self.rfi()
        self.assertEqual(self.rfi().ref, 'RFI-002')

    def test_records_not_yet_on_the_base_still_number_the_old_way(self):
        with org_context(self.org_a):
            i1 = Issue.objects.create(project=self.pa, title='Snag')
            i2 = Issue.objects.create(project=self.pa, title='Snag 2')
        self.assertEqual((i1.reference, i2.reference), ('IS-001', 'IS-002'))

    def test_cannot_save_against_another_orgs_project(self):
        with org_context(self.org_a), self.assertRaises(PermissionError):
            RFI(project=self.pb, title='x', question='?').save(actor=self.alice)


class IndexTests(RecordTestBase):
    def idx(self, rfi, manager='objects'):
        with org_context(self.org_a):
            return getattr(RecordIndex, manager).get(object_id=rfi.pk, record_type='rfis.rfi')

    def test_created_record_is_indexed(self):
        due = timezone.localdate() + timedelta(days=3)
        r = self.rfi(ball_in_court_user=self.carla, due_date=due)
        idx = self.idx(r)
        self.assertEqual((idx.ref, idx.title, idx.status, idx.status_label, idx.is_open),
                         ('RFI-001', 'Which veneer?', 'OPEN', 'Open', True))
        self.assertEqual((idx.project, idx.ball_in_court_user, idx.due_date, idx.visibility),
                         (self.pa, self.carla, due, 'SHARED'))
        self.assertEqual(idx.url, r.get_absolute_url())
        self.assertEqual(idx.record, r)

    def test_index_follows_updates_transitions_and_soft_delete(self):
        from core.workflow import apply_transition
        r = self.rfi(ball_in_court_user=self.carla)
        with org_context(self.org_a):
            r.title, r.is_shared = 'Which oak veneer?', False
            r.save(actor=self.alice)
            self.assertEqual((self.idx(r).title, self.idx(r).visibility), ('Which oak veneer?', 'INTERNAL'))
            apply_transition(r, 'respond', self.carla, response='Smoked oak')
            idx = self.idx(r)
            # Answered: the ball is back with whoever raised it, to accept or reopen
            self.assertEqual((idx.status, idx.status_label, idx.is_open, idx.ball_in_court_user),
                             ('RESPONDED', 'Responded', True, self.alice))
            r.soft_delete(self.alice)
        with org_context(self.org_a):
            self.assertFalse(RecordIndex.objects.filter(pk=idx.pk).exists())
            self.assertIsNotNone(RecordIndex.with_deleted.get(pk=idx.pk).deleted_at)

    def test_unregistered_record_type_refuses(self):
        with self.assertRaises(ImproperlyConfigured):
            register_record_type(Issue, prefix='IS')  # not a RecordBase


class AuditTests(RecordTestBase):
    def entries(self, rfi):
        with org_context(self.org_a):
            return list(AuditEntry.objects.filter(record_idx__object_id=rfi.pk, record_idx__record_type='rfis.rfi'))

    def test_created_entry_holds_the_initial_values(self):
        r = self.rfi(ball_in_court_user=self.carla)
        (e,) = self.entries(r)
        self.assertEqual((e.action, e.actor), ('created', self.alice))
        self.assertEqual(e.changes['title'], [None, 'Which veneer?'])
        self.assertEqual(e.changes['ball_in_court_user'], [None, self.carla.pk])

    def test_updates_record_field_level_diffs_and_no_op_saves_record_nothing(self):
        r = self.rfi()
        due = timezone.localdate() + timedelta(days=5)
        with org_context(self.org_a):
            r.save(actor=self.alice)  # nothing changed
            r.title, r.due_date, r.ball_in_court_user = 'New title', due, self.carla
            r.save(actor=self.alice)
            r = RFI.objects.get(pk=r.pk)  # a fresh copy diffs against what it loaded
            r.priority = 'HIGH'
            r.save(actor=self.alice)
        created, updated, priority = self.entries(r)
        self.assertEqual(updated.action, 'updated')
        self.assertEqual(updated.changes, {'title': ['Which veneer?', 'New title'], 'due_date': [None, due.isoformat()],
                                           'ball_in_court_user': [None, self.carla.pk]})
        self.assertEqual(priority.changes, {'priority': ['NORMAL', 'HIGH']})

    def test_entries_are_append_only(self):
        r = self.rfi()
        (e,) = self.entries(r)
        with org_context(self.org_a):
            e.note = 'tampered'
            with self.assertRaises(PermissionError):
                e.save()
            with self.assertRaises(PermissionError):
                e.delete()
            for qs in (AuditEntry.objects.all(), AuditEntry.all_objects.all()):
                with self.assertRaises(PermissionError):
                    qs.update(note='tampered')
                with self.assertRaises(PermissionError):
                    qs.delete()
            self.assertEqual(AuditEntry.objects.get(pk=e.pk).note, '')

    def test_deleting_a_whole_workspace_still_removes_its_records(self):
        r = self.rfi()
        self.org_a.delete()
        self.assertFalse(AuditEntry.all_objects.filter(record_idx__object_id=r.pk).exists())
        self.assertFalse(RecordIndex.all_objects.filter(object_id=r.pk).exists())


class SoftDeleteAndRetentionTests(RecordTestBase):
    def test_soft_delete_hides_the_record_but_keeps_index_and_audit(self):
        r = self.rfi()
        with org_context(self.org_a):
            r.soft_delete(self.alice, note='Raised twice')
            self.assertFalse(RFI.objects.filter(pk=r.pk).exists())
            self.assertFalse(self.pa.rfis.exists())
            self.assertTrue(RFI.with_deleted.filter(pk=r.pk).exists())
            self.assertTrue(self.pa.has_records())  # still a row: the project must be archived, not deleted
            actions = list(AuditEntry.objects.filter(record_idx__object_id=r.pk).values_list('action', 'note'))
        self.assertEqual(actions, [('created', ''), ('deleted', 'Raised twice')])
        self.login(self.alice)
        self.assertEqual(self.client.get(self.url('rfi_detail', self.org_a, self.pa.pk, r.pk)).status_code, 404)

    def test_restore_brings_it_back(self):
        r = self.rfi()
        with org_context(self.org_a):
            r.soft_delete(self.alice)
            r.restore(self.alice)
            self.assertTrue(RFI.objects.filter(pk=r.pk).exists())
            self.assertTrue(RecordIndex.objects.filter(object_id=r.pk).exists())

    def test_purge_needs_soft_delete_no_legal_hold_and_retention_passed(self):
        r = self.rfi()
        with org_context(self.org_a):
            with self.assertRaises(PurgeRefused):
                r.purge(self.alice)  # not deleted yet
            r.legal_hold = True
            r.soft_delete(self.alice)
            with self.assertRaises(PurgeRefused):
                r.purge(self.alice)
            r.legal_hold, r.retain_until = False, timezone.localdate() + timedelta(days=1)
            r.save(actor=self.alice)
            with self.assertRaises(PurgeRefused):
                r.purge(self.alice)
            self.assertTrue(RFI.with_deleted.filter(pk=r.pk).exists())
            r.retain_until = timezone.localdate()
            r.save(actor=self.alice)
            pk = r.pk
            r.purge(self.alice, note='Retention period over')
            self.assertFalse(RFI.all_objects.filter(pk=pk).exists())
            idx = RecordIndex.with_deleted.get(object_id=pk, record_type='rfis.rfi')
            self.assertIsNotNone(idx.purged_at)
            self.assertEqual(list(idx.audit.values_list('action', flat=True))[-1], 'purged')
            self.assertTrue(self.pa.has_records())  # its index row and audit trail remain

    def test_bare_delete_is_refused(self):
        r = self.rfi()
        with org_context(self.org_a), self.assertRaises(PurgeRefused):
            r.delete()


class OptimisticLockTests(RecordTestBase):
    def test_saving_a_stale_copy_raises_and_keeps_the_newer_edit(self):
        r = self.rfi()
        with org_context(self.org_a):
            mine, theirs = RFI.objects.get(pk=r.pk), RFI.objects.get(pk=r.pk)
            theirs.title = 'Their edit'
            theirs.save(actor=self.alice)
            mine.title = 'My edit'
            with self.assertRaises(RecordConflict):
                mine.save(actor=self.alice)
            self.assertEqual(mine.version, 1)  # unchanged, so a retry after reload is clean
            fresh = RFI.objects.get(pk=r.pk)
        self.assertEqual((fresh.title, fresh.version), ('Their edit', 2))

    def test_edit_form_refuses_to_overwrite_a_newer_version(self):
        r = self.rfi()
        with org_context(self.org_a):
            opened_at = RFI.objects.get(pk=r.pk).version
            r.title = 'Changed meanwhile'
            r.save(actor=self.alice)
        self.login(self.alice)
        resp = self.client.post(self.url('rfi_edit', self.org_a, self.pa.pk, r.pk), {
            'title': 'My edit', 'question': '?', 'category': 'DESIGN', 'priority': 'NORMAL', 'cost_impact': 'TBC',
            'version': opened_at})
        self.assertContains(resp, 'Someone else changed this')
        with org_context(self.org_a):
            self.assertEqual(RFI.objects.get(pk=r.pk).title, 'Changed meanwhile')

    def test_edit_form_saves_when_current(self):
        r = self.rfi()
        with org_context(self.org_a):
            form = RFIForm(instance=RFI.objects.get(pk=r.pk), project=self.pa)
            self.assertEqual(form['version'].value(), 1)
