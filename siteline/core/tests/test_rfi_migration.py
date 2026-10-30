"""
rfis 0003-0005 on seeded data: forward keeps every RFI's number, makes its ref, index row,
'created' audit entry and per-project sequence; backward puts is_shared back and removes them.
A TransactionTestCase because SQLite can't alter tables inside the test's transaction.
"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from tenancy.context import org_context

BEFORE = [('rfis', '0002_alter_rfi_project'), ('core', '0001_records')]  # core stays; RFIs go back
AFTER = [('rfis', '0005_remove_rfi_is_shared'), ('core', '0002_audit_actor_name_retention')]


class RFIMigrationTests(TransactionTestCase):
    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        self.migrate(executor.loader.graph.leaf_nodes())  # leave the schema as every other test expects
        super().tearDown()

    def seed(self, apps):
        Organization = apps.get_model('tenancy', 'Organization')
        Project = apps.get_model('projects', 'Project')
        User = apps.get_model('accounts', 'User')
        RFI = apps.get_model('rfis', 'RFI')
        ann = User.objects.create(email='ann@a.test', full_name='Ann', password='!')
        cat = User.objects.create(email='cat@a.test', full_name='Cat', password='!')
        alpha = Organization.objects.create(name='Alpha', slug='alpha')
        bravo = Organization.objects.create(name='Bravo', slug='bravo')
        pa = Project.objects.create(organization=alpha, number=1, name='Alpha job', stage='LIVE')
        pb = Project.objects.create(organization=bravo, number=1, name='Bravo job', stage='LIVE')
        rows = [(alpha, pa, 1, 'First', True, 'OPEN'), (alpha, pa, 2, 'Second', False, 'CLOSED'),
                (alpha, pa, 7, 'Seventh', True, 'RESPONDED'), (bravo, pb, 3, 'Bravo third', True, 'OPEN')]
        for org, project, number, subject, shared, status in rows:
            RFI.objects.create(organization=org, project=project, number=number, subject=subject, question='?',
                               is_shared=shared, status=status, raised_by=ann, assigned_to=cat)
        return alpha, pa, pb, ann, cat

    def test_forward_and_backward(self):
        old = self.migrate(BEFORE)
        alpha, pa, pb, ann, cat = self.seed(old)
        ids = dict(old.get_model('rfis', 'RFI').objects.values_list('subject', 'pk'))

        new = self.migrate(AFTER)
        RFI = new.get_model('rfis', 'RFI')
        got = {r.title: (r.number, r.ref, r.visibility, r.created_by_id, r.ball_in_court_user_id, r.version)
               for r in RFI.objects.all()}
        self.assertEqual(got, {
            'First': (1, 'RFI-001', 'SHARED', ann.pk, cat.pk, 1),
            'Second': (2, 'RFI-002', 'INTERNAL', ann.pk, cat.pk, 1),
            'Seventh': (7, 'RFI-007', 'SHARED', ann.pk, cat.pk, 1),
            'Bravo third': (3, 'RFI-003', 'SHARED', ann.pk, cat.pk, 1),
        })
        seqs = dict(new.get_model('core', 'NumberSequence').objects.values_list('project_id', 'next_value'))
        self.assertEqual(seqs, {pa.pk: 8, pb.pk: 4})
        index = {i.object_id: (i.ref, i.status, i.is_open, i.visibility, i.organization_id, i.url)
                 for i in new.get_model('core', 'RecordIndex').objects.all()}
        self.assertEqual(index[ids['First']], ('RFI-001', 'OPEN', True, 'SHARED', alpha.pk,
                                               f'/alpha/projects/{pa.pk}/rfis/{ids["First"]}/'))
        self.assertEqual(index[ids['Second']][1:4], ('CLOSED', False, 'INTERNAL'))
        self.assertEqual(len(index), 4)
        audit = list(new.get_model('core', 'AuditEntry').objects.values_list('action', 'actor_id', 'actor_name'))
        self.assertEqual(audit, [('created', ann.pk, 'Ann')] * 4)  # core 0002 fills the names in

        # The live code carries on numbering after the highest migrated number
        from projects.models import Project
        from rfis.models import RFI as LiveRFI
        from tenancy.models import Organization
        with org_context(Organization.objects.get(pk=alpha.pk)):
            r = LiveRFI(project=Project.objects.get(pk=pa.pk), title='After the move', question='?')
            r.save()
        self.assertEqual(r.ref, 'RFI-008')

        back = self.migrate(BEFORE)
        RFI = back.get_model('rfis', 'RFI')
        self.assertEqual({r.subject: (r.number, r.is_shared, r.raised_by_id, r.assigned_to_id)
                          for r in RFI.objects.exclude(subject='After the move')}, {
            'First': (1, True, ann.pk, cat.pk), 'Second': (2, False, ann.pk, cat.pk),
            'Seventh': (7, True, ann.pk, cat.pk), 'Bravo third': (3, True, ann.pk, cat.pk),
        })
        for model in ('RecordIndex', 'AuditEntry', 'NumberSequence'):
            self.assertFalse(back.get_model('core', model).objects.exists(), model)

    def test_empty_database_migrates_both_ways(self):
        self.migrate(BEFORE)
        apps = self.migrate(AFTER)
        self.assertFalse(apps.get_model('core', 'RecordIndex').objects.exists())
        self.migrate(BEFORE)
