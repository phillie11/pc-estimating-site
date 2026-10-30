"""The record tables follow the tenancy rules: one company never sees another's, and no organisation means no rows."""
from types import SimpleNamespace

from core.access import needs_me, visible_index
from core.models import AuditEntry, NumberSequence, RecordIndex, RecordLink, link, unlink
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership

from .base import RecordTestBase


class RecordTenancyTests(RecordTestBase):
    def setUp(self):
        super().setUp()
        self.a1 = self.rfi(ball_in_court_user=self.carla)
        self.a2 = self.rfi()
        self.b1 = self.rfi(project=self.pb, org=self.org_b, actor=self.bob)
        with org_context(self.org_a):
            link(self.a1, self.a2, actor=self.alice)

    def test_other_org_sees_none_of_the_rows(self):
        with org_context(self.org_b):
            self.assertEqual(list(RecordIndex.objects.values_list('ref', flat=True)), ['RFI-001'])
            self.assertEqual(RecordIndex.objects.get().object_id, self.b1.pk)
            self.assertFalse(RecordLink.objects.exists())
            self.assertEqual(AuditEntry.objects.count(), 1)
            self.assertEqual(NumberSequence.objects.count(), 1)
        with org_context(self.org_a):
            self.assertEqual(RecordIndex.objects.count(), 2)
            self.assertEqual(RecordLink.objects.count(), 1)

    def test_fails_closed_without_an_organisation(self):
        for model in (RecordIndex, RecordLink, AuditEntry, NumberSequence):
            with self.subTest(model=model.__name__), self.assertRaises(TenantContextMissing):
                list(model.objects.all())
        with self.assertRaises(TenantContextMissing):
            RecordIndex.with_deleted.count()

    def test_cannot_link_across_organisations(self):
        with org_context(self.org_a), self.assertRaises(PermissionError):
            link(self.a1, self.b1)

    def test_visible_index_and_needs_me_follow_project_and_visibility_rules(self):
        with org_context(self.org_a):
            self.a2.is_shared = False
            self.a2.save(actor=self.alice)
            hidden_project = self.rfi(project=self.pa2, ball_in_court_user=self.carla)  # carla isn't on pa2
            carla = SimpleNamespace(user=self.carla, membership=Membership.objects.get(user=self.carla))
            alice = SimpleNamespace(user=self.alice, membership=Membership.objects.get(user=self.alice))
            self.assertEqual(set(visible_index(carla).values_list('object_id', flat=True)), {self.a1.pk})
            self.assertEqual(set(visible_index(alice).values_list('object_id', flat=True)),
                             {self.a1.pk, self.a2.pk, hidden_project.pk})
            self.assertEqual([i.ref for i in needs_me(carla)], ['RFI-001'])


class LinkTests(RecordTestBase):
    def test_link_and_unlink_are_audited_on_both_ends(self):
        a, b = self.rfi(), self.rfi()
        with org_context(self.org_a):
            made = link(a, b, RecordLink.DERIVED_FROM, actor=self.alice)
            self.assertEqual(link(a, b, RecordLink.DERIVED_FROM), made)  # twice is harmless
            with self.assertRaises(PermissionError):
                made.save()  # links aren't edited
            with self.assertRaises(PermissionError):
                made.delete()  # only through unlink()
            with self.assertRaises(PermissionError):
                RecordLink.objects.all().delete()
            with self.assertRaises(ValueError):
                link(a, a)
            self.assertTrue(unlink(a, b, RecordLink.DERIVED_FROM, actor=self.alice))
            self.assertFalse(RecordLink.objects.exists())
            for r, other in ((a, b), (b, a)):
                entries = list(AuditEntry.objects.filter(record_idx__object_id=r.pk)
                               .exclude(action='created').values_list('action', 'changes'))
                self.assertEqual(entries, [('linked', {'link': [None, f'derived_from {other.ref}']}),
                                           ('unlinked', {'link': [f'derived_from {other.ref}', None]})])
