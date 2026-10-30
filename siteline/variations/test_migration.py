"""
variations 0003-0005 on seeded data from two companies: forward keeps every variation's number,
renames raised_by to created_by, sets visibility (drafts internal, the rest shared), passes the
ball to the client while submitted, and makes its ref, index row, 'created' audit entry,
per-project sequence and a RecordLink to its RFI; backward undoes all of it.
A TransactionTestCase because SQLite can't alter tables inside the test's transaction.
"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from tenancy.context import org_context

# core and RFIs stay where they are (already on the record base); only variations move
STAY = [('core', '0002_audit_actor_name_retention'), ('rfis', '0005_remove_rfi_is_shared')]
BEFORE = [('variations', '0002_alter_variation_project'), *STAY]
AFTER = [('variations', '0009_strip_sub_variation_source'), *STAY]  # latest, so the live model below matches


class MigrateMixin:
    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        self.migrate(executor.loader.graph.leaf_nodes())  # leave the schema as every other test expects
        super().tearDown()


class VariationMigrationTests(MigrateMixin, TransactionTestCase):

    def seed(self, apps):
        Organization = apps.get_model('tenancy', 'Organization')
        Project = apps.get_model('projects', 'Project')
        ProjectMember = apps.get_model('projects', 'ProjectMember')
        User = apps.get_model('accounts', 'User')
        RFI = apps.get_model('rfis', 'RFI')
        Variation = apps.get_model('variations', 'Variation')
        ContentType = apps.get_model('contenttypes', 'ContentType')
        RecordIndex = apps.get_model('core', 'RecordIndex')
        ann = User.objects.create(email='ann@a.test', full_name='Ann', password='!')
        cli = User.objects.create(email='cli@a.test', full_name='Cli', password='!')
        alpha = Organization.objects.create(name='Alpha', slug='alpha')
        bravo = Organization.objects.create(name='Bravo', slug='bravo')
        pa = Project.objects.create(organization=alpha, number=1, name='Alpha job', stage='LIVE')
        pb = Project.objects.create(organization=bravo, number=1, name='Bravo job', stage='LIVE')
        ProjectMember.objects.create(organization=alpha, project=pa, user=cli, role='CLIENT')
        rfi = RFI.objects.create(organization=alpha, project=pa, number=4, ref='RFI-004', title='Veneer?', question='?',
                                 status='OPEN', visibility='SHARED', created_by=ann)
        ct, _ = ContentType.objects.get_or_create(app_label='rfis', model='rfi')
        RecordIndex.objects.create(organization=alpha, content_type=ct, object_id=rfi.pk, record_type='rfis.rfi',
                                   project=pa, ref='RFI-004', title='Veneer?', status='OPEN', visibility='SHARED',
                                   created_at=rfi.created_at, updated_at=rfi.updated_at)
        rows = [(alpha, pa, 1, 'Draft one', 'DRAFT', None), (alpha, pa, 2, 'Submitted one', 'SUBMITTED', rfi),
                (alpha, pa, 5, 'Approved five', 'APPROVED', None), (alpha, pa, 6, 'Withdrawn six', 'WITHDRAWN', None),
                (bravo, pb, 3, 'Bravo three', 'SUBMITTED', None)]
        for org, project, number, title, status, linked in rows:
            Variation.objects.create(organization=org, project=project, number=number, title=title, description='x',
                                     status=status, raised_by=ann, rfi=linked)
        return alpha, pa, pb, ann, cli, rfi

    def test_forward_and_backward(self):
        old = self.migrate(BEFORE)
        alpha, pa, pb, ann, cli, rfi = self.seed(old)
        ids = dict(old.get_model('variations', 'Variation').objects.values_list('title', 'pk'))

        new = self.migrate(AFTER)
        Variation = new.get_model('variations', 'Variation')
        got = {v.title: (v.number, v.ref, v.visibility, v.created_by_id, v.ball_in_court_user_id, v.version)
               for v in Variation.objects.all()}
        self.assertEqual(got, {
            'Draft one': (1, 'VO-001', 'INTERNAL', ann.pk, None, 1),
            'Submitted one': (2, 'VO-002', 'SHARED', ann.pk, cli.pk, 1),
            'Approved five': (5, 'VO-005', 'SHARED', ann.pk, None, 1),
            'Withdrawn six': (6, 'VO-006', 'SHARED', ann.pk, None, 1),
            'Bravo three': (3, 'VO-003', 'SHARED', ann.pk, None, 1),  # Bravo has no client member
        })
        seqs = dict(new.get_model('core', 'NumberSequence').objects.filter(key='variations.variation')
                    .values_list('project_id', 'next_value'))
        self.assertEqual(seqs, {pa.pk: 7, pb.pk: 4})
        RecordIndex = new.get_model('core', 'RecordIndex')
        index = {i.object_id: (i.ref, i.status, i.is_open, i.visibility, i.organization_id, i.url, i.ball_in_court_user_id)
                 for i in RecordIndex.objects.filter(record_type='variations.variation')}
        self.assertEqual(index[ids['Submitted one']], ('VO-002', 'SUBMITTED', True, 'SHARED', alpha.pk,
                                                       f'/alpha/projects/{pa.pk}/variations/{ids["Submitted one"]}/',
                                                       cli.pk))
        self.assertEqual(index[ids['Draft one']][1:4], ('DRAFT', False, 'INTERNAL'))
        self.assertEqual(len(index), 5)
        links = [(lk.from_idx.ref, lk.to_idx.ref, lk.kind) for lk in new.get_model('core', 'RecordLink').objects.all()]
        self.assertEqual(links, [('VO-002', 'RFI-004', 'derived_from')])
        AuditEntry = new.get_model('core', 'AuditEntry')
        self.assertEqual(sorted(AuditEntry.objects.filter(action='created').values_list('actor_id', flat=True)),
                         [ann.pk] * 5)
        self.assertEqual(AuditEntry.objects.filter(action='linked').count(), 2)  # one on each end

        # The live code carries on numbering after the highest migrated number, through the record base
        from projects.models import Project
        from tenancy.models import Organization
        from variations.models import Variation as LiveVariation
        with org_context(Organization.objects.get(pk=alpha.pk)):
            v = LiveVariation(project=Project.objects.get(pk=pa.pk), title='After the move', description='x')
            v.save()
        self.assertEqual(v.ref, 'VO-007')

        back = self.migrate(BEFORE)
        Variation = back.get_model('variations', 'Variation')
        self.assertEqual({v.title: (v.number, v.status, v.raised_by_id, v.rfi_id)
                          for v in Variation.objects.exclude(title='After the move')}, {
            'Draft one': (1, 'DRAFT', ann.pk, None), 'Submitted one': (2, 'SUBMITTED', ann.pk, rfi.pk),
            'Approved five': (5, 'APPROVED', ann.pk, None), 'Withdrawn six': (6, 'WITHDRAWN', ann.pk, None),
            'Bravo three': (3, 'SUBMITTED', ann.pk, None),
        })
        self.assertFalse(hasattr(Variation, 'visibility'))
        RecordIndex = back.get_model('core', 'RecordIndex')
        self.assertEqual(list(RecordIndex.objects.values_list('ref', flat=True)), ['RFI-004'])  # the RFI's stays
        self.assertFalse(back.get_model('core', 'RecordLink').objects.exists())
        self.assertFalse(back.get_model('core', 'AuditEntry').objects.exists())
        self.assertFalse(back.get_model('core', 'NumberSequence').objects.filter(key='variations.variation').exists())

        # And forward again on the same data
        again = self.migrate(AFTER)
        self.assertEqual(again.get_model('core', 'RecordLink').objects.count(), 1)

    def test_empty_database_migrates_both_ways(self):
        self.migrate(BEFORE)
        apps = self.migrate(AFTER)
        self.assertFalse(apps.get_model('core', 'RecordIndex').objects.exists())
        self.migrate(BEFORE)


class RespondByBackfillTests(MigrateMixin, TransactionTestCase):
    """variations 0007: submitted variations get due_date = submitted + 14 days (and their index row); reversible."""
    BEFORE = [('variations', '0006_decision_evidence'), *STAY]
    AFTER = [('variations', '0007_backfill_respond_by'), *STAY]

    def test_forward_and_backward(self):
        import datetime

        old = self.migrate(self.BEFORE)
        Organization, Project = old.get_model('tenancy', 'Organization'), old.get_model('projects', 'Project')
        Variation, RecordIndex = old.get_model('variations', 'Variation'), old.get_model('core', 'RecordIndex')
        ContentType = old.get_model('contenttypes', 'ContentType')
        org = Organization.objects.create(name='Alpha', slug='alpha')
        p = Project.objects.create(organization=org, number=1, name='Job', stage='LIVE')
        ct, _ = ContentType.objects.get_or_create(app_label='variations', model='variation')
        sent = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.timezone.utc)
        agreed = datetime.date(2026, 10, 5)
        rows = {}
        for n, status, submitted, due in [(1, 'SUBMITTED', sent, None), (2, 'SUBMITTED', sent, agreed),
                                          (3, 'DRAFT', None, None), (4, 'APPROVED', sent, None)]:
            v = Variation.objects.create(organization=org, project=p, number=n, ref=f'VO-00{n}', title=f'V{n}',
                                         description='x', status=status, submitted_at=submitted, due_date=due,
                                         visibility='SHARED')
            RecordIndex.objects.create(organization=org, content_type=ct, object_id=v.pk,
                                       record_type='variations.variation', project=p, ref=v.ref, title=v.title,
                                       status=status, visibility='SHARED', due_date=due, created_at=sent,
                                       updated_at=sent)
            rows[n] = v.pk

        new = self.migrate(self.AFTER)
        due = dict(new.get_model('variations', 'Variation').objects.values_list('number', 'due_date'))
        self.assertEqual(due, {1: datetime.date(2026, 10, 15), 2: agreed, 3: None, 4: None})
        idx = dict(new.get_model('core', 'RecordIndex').objects.values_list('object_id', 'due_date'))
        self.assertEqual(idx[rows[1]], datetime.date(2026, 10, 15))

        back = self.migrate(self.BEFORE)
        due = dict(back.get_model('variations', 'Variation').objects.values_list('number', 'due_date'))
        self.assertEqual(due, {1: None, 2: agreed, 3: None, 4: None})
        idx = dict(back.get_model('core', 'RecordIndex').objects.values_list('object_id', 'due_date'))
        self.assertIsNone(idx[rows[1]])


class StripSubVariationSourceTests(MigrateMixin, TransactionTestCase):
    """variations 0009: the auto-added "(From SV-nnn, sub)" leaves client-visible descriptions; reversible."""
    BEFORE = [('variations', '0008_variation_source_note'), ('delivery', '0003_subvariation_change_event'), *STAY]
    AFTER = [('variations', '0009_strip_sub_variation_source'), ('delivery', '0003_subvariation_change_event'), *STAY]

    def test_forward_and_backward(self):
        old = self.migrate(self.BEFORE)
        Organization, Project = old.get_model('tenancy', 'Organization'), old.get_model('projects', 'Project')
        Variation, SubVariation = old.get_model('variations', 'Variation'), old.get_model('delivery', 'SubVariation')
        Subcontractor = old.get_model('directory', 'Subcontractor')
        org = Organization.objects.create(name='Alpha', slug='alpha')
        p = Project.objects.create(organization=org, number=1, name='Job', stage='LIVE')
        sub = Subcontractor.objects.create(organization=org, company_name='Lund Joinery Ltd')
        texts = {
            1: 'Two extra shelves.\n\n(From SV-002, Lund Joinery Ltd)',     # auto-added, SV linked: stripped
            2: 'Re-veneer.\n\n(From SV-009, Lund Joinery Ltd)',              # names an SV not linked to it: kept
            3: 'Typed by hand (From SV-002, Lund Joinery Ltd) in the middle',  # not the added suffix: kept
            4: 'Plain description.',
        }
        vos = {n: Variation.objects.create(organization=org, project=p, number=n, ref=f'VO-00{n}', title=f'V{n}',
                                           description=t, status='SUBMITTED', visibility='SHARED')
               for n, t in texts.items()}
        for number, vo in ((2, vos[1]), (3, vos[2]), (4, vos[3])):
            SubVariation.objects.create(organization=org, project=p, subcontractor=sub, number=number, title='x',
                                        description='x', value='1.00', status='APPROVED', client_variation=vo)

        new = self.migrate(self.AFTER)
        got = {v.number: (v.description, v.source_note) for v in new.get_model('variations', 'Variation').objects.all()}
        self.assertEqual(got[1], ('Two extra shelves.', 'From SV-002, Lund Joinery Ltd'))
        for n in (2, 3, 4):
            self.assertEqual(got[n], (texts[n], ''), n)

        back = self.migrate(self.BEFORE)
        got = dict(back.get_model('variations', 'Variation').objects.values_list('number', 'description'))
        self.assertEqual(got, texts)
