"""
Regression tests from the independent review of the record base (H1, H2, M1, M2 and the lows),
each failing on the first version and passing now.
"""
import json
from unittest import mock

from django.core import serializers
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from core import models as core_models
from core.exceptions import RecordConflict
from core.models import AuditEntry, RecordIndex, RecordLink, link, unlink
from rfis.models import RFI
from tenancy.context import org_context
from tenancy.models import Membership

from .base import RecordTestBase, make_user


def entries(rfi):
    return AuditEntry.all_objects.filter(record_idx__object_id=rfi.pk, record_idx__record_type='rfis.rfi')


class UserDeletionTests(RecordTestBase):
    """H1: deleting a user nulls their FKs on audit rows and links, and the trail still names them."""

    def test_user_with_audit_entries_can_be_deleted_and_is_still_named(self):
        dave = make_user('dave@a.test', 'Dave Davies')
        Membership.objects.create(organization=self.org_a, user=dave, role=Membership.ROLE_MEMBER)
        r = self.rfi(actor=dave)
        dave.delete()
        (e,) = entries(r)
        self.assertEqual((e.actor_id, e.actor_name), (None, 'Dave Davies'))

    def test_user_who_only_made_a_link_can_be_deleted(self):
        erin = make_user('erin@a.test', 'Erin E')
        r1, r2 = self.rfi(), self.rfi()
        with org_context(self.org_a):
            link(r1, r2, actor=erin)
        erin.delete()
        self.assertIsNone(RecordLink.all_objects.get().created_by_id)

    def test_other_updates_are_still_refused(self):
        self.rfi()
        with self.assertRaises(PermissionError):
            AuditEntry.all_objects.update(actor=self.bob)  # a user, not None
        with self.assertRaises(PermissionError):
            AuditEntry.all_objects.update(actor=None, note='')  # None for the user, plus something else


class AppendOnlyBypassTests(RecordTestBase):
    """H2: no way to UPDATE an existing audit row or link through the ORM or loaddata."""

    def test_new_instance_with_an_existing_pk_cannot_overwrite_an_entry(self):
        r = self.rfi()
        e = entries(r).get()
        with org_context(self.org_a):
            forged = AuditEntry(pk=e.pk, organization_id=e.organization_id, record_idx_id=e.record_idx_id,
                                action='FORGED')
            with self.assertRaises(IntegrityError), transaction.atomic():
                forged.save()
        self.assertEqual(AuditEntry.all_objects.get(pk=e.pk).action, 'created')

    def test_new_instance_with_an_existing_pk_cannot_overwrite_a_link(self):
        r1, r2, r3 = self.rfi(), self.rfi(), self.rfi()
        with org_context(self.org_a):
            made = link(r1, r2)
            forged = RecordLink(pk=made.pk, from_idx=RecordIndex.objects.get(object_id=r1.pk),
                                to_idx=RecordIndex.objects.get(object_id=r3.pk), created_at=made.created_at)
            with self.assertRaises(IntegrityError), transaction.atomic():
                forged.save()
        self.assertEqual(RecordLink.all_objects.get(pk=made.pk).to_idx.object_id, r2.pk)

    def test_upserts_are_refused(self):
        r = self.rfi()
        e = entries(r).get()
        copy = AuditEntry(pk=e.pk, organization_id=e.organization_id, record_idx_id=e.record_idx_id, action='FORGED')
        for qs in (AuditEntry.all_objects, RecordLink.all_objects):
            with self.assertRaises(PermissionError):
                qs.bulk_create([copy], update_conflicts=True, unique_fields=['id'], update_fields=['action'])

    def test_loaddata_cannot_overwrite_an_existing_row(self):
        r = self.rfi()
        e = entries(r).get()
        data = json.loads(serializers.serialize('json', [e]))
        data[0]['fields']['action'] = 'FORGED'
        (obj,) = serializers.deserialize('json', json.dumps(data))
        with self.assertRaises(PermissionError):
            obj.save()  # raw, as loaddata and import_data do
        self.assertEqual(AuditEntry.all_objects.get(pk=e.pk).action, 'created')
        data[0]['fields']['action'] = 'created'  # re-loading it exactly as it is changes nothing, so it's allowed
        (obj,) = serializers.deserialize('json', json.dumps(data))
        obj.save()
        data[0]['pk'] = e.pk + 1000  # a new row (an import into an empty database) still loads
        (obj,) = serializers.deserialize('json', json.dumps(data))
        obj.save()
        self.assertTrue(AuditEntry.all_objects.filter(pk=e.pk + 1000).exists())


class StaleCopyTests(RecordTestBase):
    """M1: a stale copy never falls through to INSERT."""

    def test_stale_copy_cannot_bring_back_a_purged_record(self):
        r = self.rfi(retain_until=timezone.localdate())
        with org_context(self.org_a):
            stale = RFI.with_deleted.get(pk=r.pk)
            fresh = RFI.with_deleted.get(pk=r.pk)
            fresh.soft_delete(self.alice)
            fresh.purge(self.alice)
            stale.title = 'edited'
            with self.assertRaises(RecordConflict):
                stale.save(actor=self.alice)
        self.assertFalse(RFI.all_objects.filter(pk=r.pk).exists())
        self.assertIsNotNone(RecordIndex.all_objects.get(object_id=r.pk).purged_at)


class NumberingHardeningTests(RecordTestBase):
    """M2 and the numbering lows."""

    def test_first_number_locks_the_project_row(self):
        from projects.models import Project
        real = Project.all_objects.select_for_update
        with mock.patch.object(type(Project.all_objects), 'select_for_update', autospec=True,
                               side_effect=lambda mgr, *a, **k: real(*a, **k)) as locked:
            self.rfi()  # first of its type: the project row is locked
            self.rfi()  # the sequence exists: it isn't
        self.assertEqual(locked.call_count, 1)

    def test_a_deadlock_is_retried_once(self):
        real, calls = core_models._take_number, []

        def flaky(*args):
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError('deadlock detected')
            return real(*args)

        with mock.patch.object(core_models, '_take_number', side_effect=flaky):
            r = self.rfi()
        self.assertEqual((len(calls), r.ref), (2, 'RFI-001'))

    def test_other_database_errors_are_not_retried(self):
        with mock.patch.object(core_models, '_take_number', side_effect=OperationalError('disk I/O error')) as take:
            with self.assertRaises(OperationalError):
                self.rfi()
        self.assertEqual(take.call_count, 1)

    def test_a_number_set_by_hand_moves_the_sequence_past_it(self):
        self.rfi()
        with org_context(self.org_a):
            RFI(project=self.pa, title='Manual', question='?', number=2).save(actor=self.alice)
            RFI(project=self.pa, title='Way ahead', question='?', number=9).save(actor=self.alice)
        self.assertEqual(self.rfi().ref, 'RFI-010')

    def test_a_failed_first_save_leaves_the_copy_unsaved(self):
        with org_context(self.org_a):
            r = RFI(project=self.pa, title='One', question='?')
            with mock.patch('core.models.index_record', side_effect=RuntimeError('index down')):
                with self.assertRaises(RuntimeError):
                    r.save(actor=self.alice)
            self.assertEqual((r.pk, r.number, r.ref, r._state.adding), (None, None, '', True))
            r.save(actor=self.alice)  # a retry is a clean first save
        self.assertEqual((r.ref, entries(r).count()), ('RFI-001', 1))


class AuditDetailTests(RecordTestBase):
    def test_update_fields_audits_only_the_fields_saved(self):
        r = self.rfi()
        with org_context(self.org_a):
            r.title, r.priority = 'Saved', 'HIGH'
            r.save(actor=self.alice, update_fields=['title'])
            self.assertEqual(RFI.objects.get(pk=r.pk).priority, 'NORMAL')
            self.assertEqual(entries(r).last().changes, {'title': ['Which veneer?', 'Saved']})
            r.save(actor=self.alice)  # the unsaved priority change is audited when it is saved
        self.assertEqual(entries(r).last().changes, {'priority': ['NORMAL', 'HIGH']})

    def test_link_entries_name_the_other_record_by_ref_only(self):
        a, b = self.rfi(title='Internal secret'), self.rfi(title='Other secret')
        with org_context(self.org_a):
            link(a, b, actor=self.alice)
            unlink(a, b, actor=self.alice)
        notes = ' '.join(AuditEntry.all_objects.values_list('note', flat=True))
        self.assertIn('RFI-001 relates to RFI-002', notes)
        self.assertNotIn('secret', notes)
