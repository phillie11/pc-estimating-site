"""
TenantModel.objects fails closed: with no current organisation a query raises
instead of returning every company's rows, and bulk writes stay inside the
current organisation. Plus every management command, run against two companies.
"""
import io
import os
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models import Count, Exists, OuterRef, Subquery
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from directory.models import Subcontractor, Trade
from projects.models import Document, DocumentRevision, FileScan, Project, ProjectMember
from projects.tests import FileStorageMixin, cloud
from rfis.models import RFI
from tenders.forms import TenderInviteForm

from .context import TenantContextMissing, org_context, unscoped
from .models import Membership, Notification, Organization
from .tests import Base, make_user


class FailClosedReadTests(Base):
    def test_every_way_of_running_a_query_raises_without_an_org(self):
        qs = Project.objects.all()  # building it is fine; running it is not
        runs = {
            'iterate': lambda: list(qs),
            'count': qs.count,
            'exists': qs.exists,
            'get': lambda: Project.objects.get(pk=self.pa.pk),
            'first': qs.first,
            'values_list': lambda: list(qs.values_list('name', flat=True)),
            'iterator': lambda: list(qs.iterator()),
            'aggregate': lambda: qs.aggregate(n=Count('pk')),
            'in_bulk': lambda: qs.in_bulk([self.pa.pk]),
            'slice': lambda: list(qs[:1]),
            'subquery': lambda: list(RFI.all_objects.filter(project__in=Project.objects.all())),
            'related manager': lambda: list(self.pa.rfis.all()),
            'get_or_create': lambda: Subcontractor.objects.get_or_create(company_name='Alpha Sparks'),
        }
        for name, run in runs.items():
            with self.assertRaises(TenantContextMissing, msg=name):
                run()

    def test_empty_querysets_and_foreign_keys_still_work_without_an_org(self):
        self.assertEqual(list(Trade.objects.none()), [])
        rfi = RFI.all_objects.get(pk=self.rfi_a.pk)
        self.assertEqual(rfi.project.name, 'Alpha secret job')  # base manager follows the FK

    def test_queryset_made_without_an_org_is_scoped_when_it_runs(self):
        """Class-level form querysets are built at import time, with no org."""
        with org_context(self.org_a):
            Trade.objects.create(name='Alpha only trade')
        qs = Trade.objects.all()
        with org_context(self.org_b):
            self.assertNotIn('Alpha only trade', [t.name for t in qs])
            self.assertTrue(all(t.organization_id == self.org_b.pk for t in qs))

    def test_form_choices_are_scoped(self):
        with org_context(self.org_b):
            choices = list(TenderInviteForm(project=self.pb).fields['trade'].queryset)
        self.assertTrue(choices)
        self.assertEqual({t.organization_id for t in choices}, {self.org_b.pk})

    def test_queryset_built_for_one_org_cannot_run_in_another(self):
        with org_context(self.org_a):
            qs = Project.objects.all()
        self.assertEqual([p.name for p in qs.all()], ['Alpha secret job'])  # still filtered to A
        with org_context(self.org_b), self.assertRaises(PermissionError):
            list(qs.all())


class GuardBypassTests(Base):
    """
    Regression tests from the independent security review: ways a query reached
    the database without passing through _guarded().
    """
    def names(self, qs):
        return sorted(p.name for p in qs)

    def test_or_and_xor_with_a_queryset_built_without_an_org(self):
        pre = Project.objects.filter(name__icontains='job')  # a class attribute, or built before org_context
        with org_context(self.org_b):
            self.assertEqual(self.names(Project.objects.filter(pk=self.pb.pk) | pre), ['Bravo job'])
            self.assertEqual(self.names(pre | Project.objects.filter(pk=self.pb.pk)), ['Bravo job'])
            self.assertEqual(self.names(pre & Project.objects.all()), ['Bravo job'])
            self.assertEqual(self.names(Project.objects.none() ^ pre), ['Bravo job'])

    def test_combining_with_all_objects_is_refused(self):
        with org_context(self.org_b):
            for combine in (lambda: Project.objects.all() | Project.all_objects.all(),
                            lambda: Project.objects.all() & Project.all_objects.all(),
                            lambda: Project.objects.order_by().union(Project.all_objects.order_by()),
                            lambda: Project.objects.none().union(Project.all_objects.order_by())):
                with self.assertRaises(PermissionError):
                    list(combine())

    def test_subquery_and_exists_built_without_an_org(self):
        """Subquery() and Exists() copy .query and never call the queryset's own guards."""
        name = Subquery(Project.objects.filter(pk=OuterRef('project')).values('name')[:1])
        has_rfis = Exists(RFI.objects.filter(project=OuterRef('pk')))
        rfi_project_names = RFI.all_objects.annotate(n=name).values_list('n', flat=True)
        projects_with_rfis = Project.all_objects.annotate(h=has_rfis).filter(h=True).values_list('name', flat=True)
        with self.assertRaises(TenantContextMissing):
            list(rfi_project_names)
        with self.assertRaises(TenantContextMissing):
            list(projects_with_rfis)
        with org_context(self.org_b):
            self.assertEqual(list(rfi_project_names.all()), [None])  # Alpha's RFI; Bravo can't read its project
            self.assertEqual(list(projects_with_rfis.all()), [])

    def test_org_is_read_when_the_sql_is_compiled_not_when_it_is_built(self):
        names = RFI.all_objects.annotate(
            n=Subquery(Project.objects.filter(pk=OuterRef('project')).values('name')[:1])).values_list('n', flat=True)
        qs = Project.objects.all()
        with org_context(self.org_a):
            self.assertEqual(list(names.all()), ['Alpha secret job'])
            self.assertEqual(self.names(qs.all()), ['Alpha secret job'])
        with org_context(self.org_b):
            self.assertEqual(list(names.all()), [None])
            self.assertEqual(self.names(qs.all()), ['Bravo job'])

    def test_subquery_built_in_one_org_cannot_run_in_another(self):
        with org_context(self.org_a):
            name = Subquery(Project.objects.filter(pk=OuterRef('project')).values('name')[:1])
        with org_context(self.org_b), self.assertRaises(PermissionError):
            list(RFI.all_objects.annotate(n=name).values_list('n', flat=True))


class EscapeHatchTests(Base):
    def test_unscoped_reads_every_company(self):
        with unscoped():
            self.assertEqual(Project.objects.count(), 2)
            self.assertEqual(sorted(p.name for p in Project.objects.all()), ['Alpha secret job', 'Bravo job'])
        self.assertEqual(Project.all_objects.count(), 2)

    def test_queryset_made_inside_unscoped_stays_unscoped(self):
        with unscoped():
            qs = Project.objects.all()
        self.assertEqual(qs.count(), 2)

    def test_org_context_inside_unscoped_wins(self):
        with unscoped(), org_context(self.org_b):
            self.assertEqual(list(Project.objects.values_list('name', flat=True)), ['Bravo job'])

    def test_unscoped_saves_need_an_explicit_org(self):
        with unscoped():
            with self.assertRaises(ImproperlyConfigured):
                Trade.objects.create(name='Nobody')
            Trade.objects.create(name='Explicit', organization=self.org_a)
        self.assertTrue(Trade.all_objects.filter(name='Explicit', organization=self.org_a).exists())


class GuardedWriteTests(Base):
    def test_update_cannot_touch_another_orgs_rows(self):
        with org_context(self.org_b):
            self.assertEqual(Project.objects.filter(pk=self.pa.pk).update(name='hijacked'), 0)
            self.assertEqual(Project.objects.update(name='Bravo renamed'), 1)
        self.assertEqual(Project.all_objects.get(pk=self.pa.pk).name, 'Alpha secret job')

    def test_update_cannot_move_rows_to_another_org(self):
        with org_context(self.org_b), self.assertRaises(PermissionError):
            Project.objects.filter(pk=self.pb.pk).update(organization=self.org_a)
        with org_context(self.org_b), self.assertRaises(PermissionError):
            Project.objects.filter(pk=self.pb.pk).update(organization_id=self.org_a.pk)
        self.assertEqual(Project.all_objects.get(pk=self.pb.pk).organization_id, self.org_b.pk)

    def test_update_and_delete_raise_without_an_org(self):
        with self.assertRaises(TenantContextMissing):
            Project.objects.update(name='everyone')
        with self.assertRaises(TenantContextMissing):
            Subcontractor.objects.all().delete()
        self.assertEqual(Project.all_objects.filter(name='everyone').count(), 0)
        self.assertTrue(Subcontractor.all_objects.filter(pk=self.sub_a.pk).exists())

    def test_delete_cannot_touch_another_orgs_rows(self):
        with org_context(self.org_b):
            self.assertEqual(Subcontractor.objects.filter(pk=self.sub_a.pk).delete()[0], 0)
            self.assertEqual(RFI.objects.all().delete()[0], 0)
            Project.objects.all().delete()
        self.assertTrue(Subcontractor.all_objects.filter(pk=self.sub_a.pk).exists())
        self.assertTrue(RFI.all_objects.filter(pk=self.rfi_a.pk).exists())
        self.assertEqual(list(Project.all_objects.values_list('name', flat=True)), ['Alpha secret job'])

    def test_bulk_update_skips_other_orgs_rows(self):
        pa = Project.all_objects.get(pk=self.pa.pk)
        pa.name = 'hijacked'
        with org_context(self.org_b):
            Project.objects.bulk_update([pa], ['name'])
        self.assertEqual(Project.all_objects.get(pk=self.pa.pk).name, 'Alpha secret job')

    def test_bulk_create_refuses_a_foreign_org_and_writes_nothing(self):
        before = Trade.all_objects.count()
        with org_context(self.org_b), self.assertRaises(PermissionError):
            Trade.objects.bulk_create([Trade(name='Mine'), Trade(name='Theirs', organization=self.org_a)])
        self.assertEqual(Trade.all_objects.count(), before)

    def test_bulk_create_sets_the_current_org(self):
        with org_context(self.org_b):
            Trade.objects.bulk_create([Trade(name='Glazing B'), Trade(name='Signage B', organization=self.org_b)])
        self.assertEqual(set(Trade.all_objects.filter(name__endswith=' B').values_list('organization', flat=True)),
                         {self.org_b.pk})

    def test_bulk_create_needs_an_org_or_unscoped(self):
        with self.assertRaises(TenantContextMissing):
            Trade.objects.bulk_create([Trade(name='Floating', organization=self.org_a)])
        with unscoped():
            with self.assertRaises(ImproperlyConfigured):
                Trade.objects.bulk_create([Trade(name='Nobody')])
            Trade.objects.bulk_create([Trade(name='Platform made', organization=self.org_a)])
        self.assertTrue(Trade.all_objects.filter(name='Platform made', organization=self.org_a).exists())


class AdminTests(Base):
    """The platform admin is deliberately cross-company and runs inside unscoped()."""
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser(email='root@platform.test', password='Root-Pass-9', full_name='Root')
        self.client.force_login(self.root, backend='django.contrib.auth.backends.ModelBackend')
        with org_context(self.org_a):
            self.pm = ProjectMember.objects.create(project=self.pa, user=self.alice, role=ProjectMember.ROLE_PM)

    def test_admin_pages_work_and_list_every_company(self):
        r = self.client.get(reverse('admin:projects_project_changelist'))
        self.assertContains(r, 'Alpha secret job')
        self.assertContains(r, 'Bravo job')
        for url in (reverse('admin:projects_project_change', args=[self.pa.pk]),
                    reverse('admin:projects_project_delete', args=[self.pa.pk]),
                    reverse('admin:projects_projectmember_change', args=[self.pm.pk]),
                    reverse('admin:projects_projectmember_add'),
                    reverse('admin:projects_activity_changelist'),
                    reverse('admin:tenancy_activityevent_changelist'),
                    reverse('admin:tenancy_organization_delete', args=[self.org_a.pk])):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_admin_edit_saves(self):
        url = reverse('admin:projects_projectmember_change', args=[self.pm.pk])
        r = self.client.post(url, {'project': self.pa.pk, 'user': self.alice.pk, 'role': ProjectMember.ROLE_CLIENT,
                                   'company_name': '', 'added_by': self.alice.pk})
        self.assertEqual(r.status_code, 302, getattr(r, 'context_data', {}).get('errors'))
        self.assertEqual(ProjectMember.all_objects.get(pk=self.pm.pk).role, ProjectMember.ROLE_CLIENT)


class SendRemindersTests(Base):
    def setUp(self):
        super().setUp()
        self.carol = make_user('carol@b.test', 'Carol B')
        Membership.objects.create(organization=self.org_b, user=self.carol, role=Membership.ROLE_MEMBER)
        late = timezone.localdate() - timedelta(days=2)
        soon = timezone.now() + timedelta(days=1)
        with org_context(self.org_a):
            RFI.objects.filter(pk=self.rfi_a.pk).update(assigned_to=self.alice, due_date=late)
            Project.objects.create(name='Alpha tender', stage=Project.STAGE_TENDERING, tender_due=soon)
        with org_context(self.org_b):
            RFI.objects.create(project=self.pb, subject='Bravo question', question='?', assigned_to=self.bob, due_date=late)
            Project.objects.create(name='Bravo tender', stage=Project.STAGE_TENDERING, tender_due=soon)

    def test_each_org_digest_goes_only_to_its_own_members(self):
        call_command('send_reminders', stdout=io.StringIO())
        by_user = {m.to[0]: m for m in mail.outbox}
        self.assertEqual(set(by_user), {'alice@a.test', 'bob@b.test', 'carol@b.test'})
        alice = by_user['alice@a.test']
        self.assertTrue(alice.subject.startswith('[Alpha Fitout]'))
        self.assertIn('Alpha tender', alice.body)
        self.assertIn('A question', alice.body)
        self.assertNotIn('Bravo', alice.body)
        for email in ('bob@b.test', 'carol@b.test'):
            body = by_user[email].body
            self.assertTrue(by_user[email].subject.startswith('[Bravo Build]'))
            self.assertIn('Bravo tender', body)
            self.assertNotIn('Alpha', body)
        self.assertIn('Bravo question', by_user['bob@b.test'].body)
        self.assertNotIn('Bravo question', by_user['carol@b.test'].body)  # not assigned to her


class FileCommandTests(FileStorageMixin, Base):
    def add_docs(self):
        revs = {}
        for org, project in ((self.org_a, self.pa), (self.org_b, self.pb)):
            with org_context(org):
                doc = Document.objects.create(project=project, title=f'{org.name} plan')
                revs[org.pk] = DocumentRevision.objects.create(
                    document=doc, original_name='plan.pdf', file=SimpleUploadedFile('plan.pdf', b'%PDF-1.4'))
        return revs

    def test_check_file_scans_keeps_each_alert_in_its_own_org(self):
        with cloud(self.cloud_dir, scan=True):
            revs = self.add_docs()
            verdicts = {revs[self.org_a.pk].file.name: 'THREATS_FOUND', revs[self.org_b.pk].file.name: 'NO_THREATS_FOUND'}
            with mock.patch('projects.files.fetch_scan_tag', side_effect=lambda f: verdicts.get(f.name)):
                call_command('check_file_scans', stdout=io.StringIO(), stderr=io.StringIO())
        for org in (self.org_a, self.org_b):
            self.assertEqual(FileScan.all_objects.get(path=revs[org.pk].file.name).organization_id, org.pk)
        alerts = Notification.all_objects.filter(title__startswith='Infected')
        self.assertTrue(alerts.exists())
        self.assertEqual(set(alerts.values_list('organization', flat=True)), {self.org_a.pk})
        a_people = set(Membership.objects.filter(organization=self.org_a).values_list('user', flat=True))
        self.assertTrue(set(alerts.values_list('user', flat=True)) <= a_people)

    def test_move_files_to_storage_copies_every_company(self):
        with self.settings(MEDIA_ROOT=self.local_dir):
            revs = self.add_docs()
        with cloud(self.cloud_dir), self.settings(MEDIA_ROOT=self.local_dir):
            out = io.StringIO()
            call_command('move_files_to_storage', stdout=out)
        self.assertIn('Copied 2,', out.getvalue())
        for rev in revs.values():
            self.assertTrue(os.path.exists(os.path.join(self.cloud_dir, rev.file.name)))


class SeedDemoTests(Base):
    def test_seed_and_reset_leave_other_companies_alone(self):
        with self.settings(MEDIA_ROOT=self.make_media()):
            call_command('seed_demo', stdout=io.StringIO())
            demo = Organization.objects.get(slug='northbank-interiors')
            seeded = Project.all_objects.filter(organization=demo).count()
            self.assertGreater(seeded, 3)
            call_command('seed_demo', '--reset', stdout=io.StringIO())
        demo = Organization.objects.get(slug='northbank-interiors')
        self.assertEqual(Project.all_objects.filter(organization=demo).count(), seeded)
        self.assertEqual(Project.all_objects.filter(organization=self.org_a).count(), 1)
        self.assertEqual(Project.all_objects.filter(organization=self.org_b).count(), 1)
        self.assertEqual(Subcontractor.all_objects.filter(organization=self.org_a).count(), 1)
        self.assertEqual(Notification.all_objects.exclude(organization=demo).count(), 0)

    def make_media(self):
        import shutil
        import tempfile
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path
