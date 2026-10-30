"""Bulk upload: preview, inline fixes, commit, the virus-scan gate and background post-processing."""
from unittest import mock

from django.test import override_settings

from documents import bulk
from documents.models import RegisterSettings, UploadBatch, UploadRow
from jobs.models import Job
from jobs.runner import Worker
from projects.models import Document, DocumentRevision, FileScan
from tenancy.context import org_context

from .base import DocBase, pdf_bytes, upload


class BulkBase(DocBase):
    def setUp(self):
        super().setUp()
        self.ga, self.ga_revs = self.doc('SLN-ABC-ZZ-02-DR-A-1001', 'GA plan', ('P01', 'P02'))
        self.login(self.staff)

    def send(self, *names, contents=None):
        files = [upload(n, (contents or {}).get(n)) for n in names]
        r = self.client.post(self.purl('bulk_upload'), {'files': files})
        with org_context(self.org_a):
            batch = UploadBatch.objects.order_by('-pk').first()
        return r, batch

    def rows(self, batch):
        with org_context(self.org_a):
            return {r.original_name: r for r in UploadRow.objects.filter(batch=batch)
                    .select_related('supersedes', 'document')}


class PreviewTests(BulkBase):
    def test_preview_classifies_each_file(self):
        same = pdf_bytes(1)
        with org_context(self.org_a):
            DocumentRevision.objects.filter(pk=self.ga_revs['P02'].pk).update(sha256=__import__('hashlib')
                                                                                .sha256(same).hexdigest())
        r, batch = self.send('SLN-ABC-ZZ-02-DR-A-1001_P03.pdf',          # new revision of GA
                             'SLN-ABC-ZZ-02-DR-A-1001-P01.pdf',          # older than current P02... already there
                             'SLN-ABC-ZZ-02-DR-A-1001_P02.pdf',          # P02 with the same content
                             'SLN-ABC-ZZ-03-DR-A-1002_S2_P01 Level 3 GA.pdf',  # new document
                             'SLN-ABC-ZZ-03-DR-A-1003_P01.pdf',
                             'SLN-ABC-ZZ-03-DR-A-1003-P01.pdf',          # the same number and rev twice
                             'scan0001.pdf',                             # nothing to go on
                             contents={'SLN-ABC-ZZ-02-DR-A-1001_P02.pdf': same})
        self.assertRedirects(r, batch.get_absolute_url())
        rows = self.rows(batch)
        p03 = rows['SLN-ABC-ZZ-02-DR-A-1001_P03.pdf']
        self.assertEqual((p03.action, p03.document, p03.supersedes.revision), ('NEW_REV', self.ga, 'P02'))
        self.assertIn('Supersedes rev P02', p03.message)
        self.assertEqual(rows['SLN-ABC-ZZ-02-DR-A-1001-P01.pdf'].action, 'CONFLICT')
        self.assertEqual(rows['SLN-ABC-ZZ-02-DR-A-1001_P02.pdf'].action, 'DUPLICATE')
        new = rows['SLN-ABC-ZZ-03-DR-A-1002_S2_P01 Level 3 GA.pdf']
        self.assertEqual((new.action, new.suitability, new.title, new.revision), ('NEW_DOC', 'S2', 'Level 3 GA', 'P01'))
        self.assertEqual(rows['SLN-ABC-ZZ-03-DR-A-1003_P01.pdf'].action, 'NEW_DOC')
        self.assertEqual(rows['SLN-ABC-ZZ-03-DR-A-1003-P01.pdf'].action, 'CONFLICT')
        self.assertEqual(rows['scan0001.pdf'].action, 'INCOMPLETE')
        page = self.client.get(batch.get_absolute_url())
        self.assertContains(page, 'Supersedes rev P02')
        self.assertContains(page, 'Already uploaded')
        self.assertContains(page, 'Add the revision.')
        # Small batches get their checksum and page count straight away
        self.assertTrue(p03.processed and len(p03.sha256) == 64 and p03.page_count == 1)

    def test_older_revision_row(self):
        _, batch = self.send('SLN-ABC-ZZ-02-DR-A-1001_P00.pdf')
        row = self.rows(batch)['SLN-ABC-ZZ-02-DR-A-1001_P00.pdf']
        self.assertEqual(row.action, 'OLDER_REV')
        self.assertIn('filed as superseded', row.message)

    def test_project_pattern_is_used(self):
        with org_context(self.org_a):
            RegisterSettings.objects.create(project=self.pa, filename_pattern='{reference} rev {revision}')
        _, batch = self.send('1203-A-100 rev C02.pdf')
        row = self.rows(batch)['1203-A-100 rev C02.pdf']
        self.assertEqual((row.reference, row.revision), ('1203-A-100', 'C02'))

    def test_blocked_file_types_are_refused(self):
        r = self.client.post(self.purl('bulk_upload'), {'files': [upload('evil.exe', b'MZ')]}, follow=True)
        self.assertContains(r, 'not allowed')
        with org_context(self.org_a):
            self.assertFalse(UploadRow.objects.exists())
            self.assertFalse(UploadBatch.objects.exists())

    def test_staff_only(self):
        for u in (self.cli, self.con, self.sub1):
            self.login(u)
            self.assertEqual(self.client.get(self.purl('bulk_upload')).status_code, 403)
        self.login(self.bidder)
        self.assertEqual(self.client.get(self.purl('bulk_upload')).status_code, 403)
        self.login(self.outsider)
        self.assertEqual(self.client.get(self.purl('bulk_upload')).status_code, 404)


class CommitTests(BulkBase):
    def commit(self, batch, include, **extra):
        data = {'action': 'commit'}
        for name, row in self.rows(batch).items():
            data.update({f'ref_{row.pk}': row.reference, f'rev_{row.pk}': row.revision,
                         f'suit_{row.pk}': row.suitability, f'title_{row.pk}': row.title, f'cat_{row.pk}': 'DRAWING'})
            if name in include:
                data[f'include_{row.pk}'] = '1'
        data.update(extra)
        return self.client.post(batch.get_absolute_url(), data, follow=True)

    def test_inline_fix_then_commit(self):
        _, batch = self.send('scan0001.pdf', 'SLN-ABC-ZZ-02-DR-A-1001_P03.pdf', 'SLN-ABC-ZZ-04-DR-A-1009_C01.pdf')
        rows = self.rows(batch)
        scan = rows['scan0001.pdf']
        data = {'action': 'save', f'ref_{scan.pk}': 'sln-abc-zz-05-dr-a-1010', f'rev_{scan.pk}': 'p01',
                f'suit_{scan.pk}': 'S2', f'title_{scan.pk}': 'Level 5 GA', f'cat_{scan.pk}': 'DRAWING'}
        self.client.post(batch.get_absolute_url(), data)
        scan = self.rows(batch)['scan0001.pdf']
        self.assertEqual((scan.action, scan.reference, scan.revision), ('NEW_DOC', 'SLN-ABC-ZZ-05-DR-A-1010', 'P01'))

        r = self.commit(batch, {'scan0001.pdf', 'SLN-ABC-ZZ-02-DR-A-1001_P03.pdf', 'SLN-ABC-ZZ-04-DR-A-1009_C01.pdf'})
        self.assertContains(r, '3 added to the register')
        with org_context(self.org_a):
            ga = Document.objects.get(pk=self.ga.pk)
            self.assertEqual(ga.current.revision, 'P03')
            self.assertEqual(DocumentRevision.objects.get(pk=self.ga_revs['P02'].pk).status, 'SUPERSEDED')
            new = Document.objects.get(reference='SLN-ABC-ZZ-05-DR-A-1010')
            self.assertEqual((new.title, new.naming, new.visibility), ('Level 5 GA', 'OK', 'INTERNAL'))
            rev = new.revisions.get()
            row = UploadRow.objects.get(pk=scan.pk)
            # The revision uses the stored (scanned) file itself, not a copy
            self.assertEqual(rev.file.name, row.file.name)
            self.assertEqual((rev.suitability, rev.sha256), ('S2', row.sha256))
            self.assertEqual(UploadBatch.objects.get(pk=batch.pk).status, UploadBatch.DONE)

    def test_conflicts_and_unaccepted_warnings_stay_behind(self):
        _, batch = self.send('SLN-ABC-ZZ-02-DR-A-1001_P02.pdf', 'X-ABC-ZZ-02-DR-A-77_P01.pdf')
        r = self.commit(batch, {'SLN-ABC-ZZ-02-DR-A-1001_P02.pdf', 'X-ABC-ZZ-02-DR-A-77_P01.pdf'})
        self.assertNotContains(r, ' added to the register.')
        self.assertContains(r, 'Tick &quot;accept&quot;')
        odd = self.rows(batch)['X-ABC-ZZ-02-DR-A-77_P01.pdf']
        self.assertEqual(odd.state, 'PENDING')
        self.assertIn('should be 4 to 6', odd.warnings)
        r = self.commit(batch, {'X-ABC-ZZ-02-DR-A-77_P01.pdf'}, **{f'accept_{odd.pk}': '1'})
        self.assertContains(r, '1 added to the register')
        with org_context(self.org_a):
            d = Document.objects.get(reference='X-ABC-ZZ-02-DR-A-77')
        self.assertEqual(d.naming, Document.NAMING_ACCEPTED)
        self.assertEqual(self.rows(batch)['SLN-ABC-ZZ-02-DR-A-1001_P02.pdf'].state, 'PENDING')

    def test_commit_then_issue_to_usual_recipients(self):
        from documents.models import Transmittal
        _, batch = self.send('SLN-ABC-ZZ-02-DR-A-1001_P03.pdf')
        self.commit(batch, {'SLN-ABC-ZZ-02-DR-A-1001_P03.pdf'}, then='issue', purpose='CONSTRUCTION')
        with org_context(self.org_a):
            tr = Transmittal.objects.get()
            self.assertEqual((tr.status, tr.purpose, tr.items.get().revision_code), ('ISSUED', 'CONSTRUCTION', 'P03'))

    def test_commit_then_start_a_transmittal(self):
        _, batch = self.send('SLN-ABC-ZZ-02-DR-A-1001_P03.pdf')
        r = self.commit(batch, {'SLN-ABC-ZZ-02-DR-A-1001_P03.pdf'}, then='transmit')
        self.assertContains(r, 'New transmittal')
        self.assertContains(r, 'checked')

    def test_cancel_removes_uncommitted_files(self):
        _, batch = self.send('SLN-ABC-ZZ-02-DR-A-1001_P03.pdf')
        row = self.rows(batch)['SLN-ABC-ZZ-02-DR-A-1001_P03.pdf']
        storage, name = row.file.storage, row.file.name
        self.assertTrue(storage.exists(name))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(batch.get_absolute_url(), {'action': 'cancel'})
        self.assertFalse(storage.exists(name))
        with org_context(self.org_a):
            self.assertEqual(UploadBatch.objects.get(pk=batch.pk).status, UploadBatch.CANCELLED)


@override_settings(STORAGE_BUCKET='test-bucket', STORAGE_MALWARE_SCAN=True)
class ScanGateTests(BulkBase):
    """With GuardDuty scanning on, a row waits as "scanning" until its file is tagged clean."""

    def test_rows_wait_until_clean_and_blocked_rows_never_go(self):
        verdicts = {}

        def fake_tag(fieldfile):
            return verdicts.get(fieldfile.name.rsplit('/', 1)[-1])

        with mock.patch('projects.files.fetch_scan_tag', side_effect=fake_tag):
            _, batch = self.send('SLN-ABC-ZZ-02-DR-A-1001_P03.pdf', 'SLN-ABC-ZZ-09-DR-A-1009_P01.pdf')
            rows = self.rows(batch)
            good, bad = rows['SLN-ABC-ZZ-02-DR-A-1001_P03.pdf'], rows['SLN-ABC-ZZ-09-DR-A-1009_P01.pdf']
            page = self.client.get(batch.get_absolute_url())
            self.assertContains(page, 'Virus check in progress')
            self.assertEqual(bulk.row_scan(good, self.org_a), bulk.SCAN_PENDING)
            data = {'action': 'commit', f'include_{good.pk}': '1', f'include_{bad.pk}': '1'}
            for row in (good, bad):
                data.update({f'ref_{row.pk}': row.reference, f'rev_{row.pk}': row.revision})
            r = self.client.post(batch.get_absolute_url(), data, follow=True)
            self.assertContains(r, 'Still being virus-checked')
            with org_context(self.org_a):
                self.assertEqual(Document.objects.get(pk=self.ga.pk).current.revision, 'P02')

            verdicts['SLN-ABC-ZZ-02-DR-A-1001_P03.pdf'] = FileScan.CLEAN
            verdicts['SLN-ABC-ZZ-09-DR-A-1009_P01.pdf'] = FileScan.INFECTED
            r = self.client.post(batch.get_absolute_url(), data, follow=True)
            self.assertContains(r, '1 added to the register')
            self.assertContains(r, 'failed the virus check')
            with org_context(self.org_a):
                self.assertEqual(Document.objects.get(pk=self.ga.pk).current.revision, 'P03')
                self.assertFalse(Document.objects.filter(reference='SLN-ABC-ZZ-09-DR-A-1009').exists())
                self.assertEqual(UploadRow.objects.get(pk=bad.pk).state, 'PENDING')


@override_settings(STORAGE_BUCKET='test-bucket', STORAGE_MALWARE_SCAN=True)
class BackgroundProcessingTests(BulkBase):
    """Batches over the inline limit leave checksum and page count to the worker, which waits for the scan."""

    def test_large_batch_is_processed_by_the_worker_after_scanning(self):
        names = [f'SLN-ABC-ZZ-0{i % 10}-DR-A-2{i:03d}_P01.pdf' for i in range(12)]
        clean = {'on': False}

        def fake_tag(fieldfile):
            return FileScan.CLEAN if clean['on'] else None

        with mock.patch('projects.files.fetch_scan_tag', side_effect=fake_tag):
            _, batch = self.send(*names, contents={n: pdf_bytes(3) for n in names})
            rows = self.rows(batch)
            self.assertTrue(all(not r.processed and not r.sha256 for r in rows.values()))
            self.assertEqual(Job.objects.filter(name='documents.file_facts').count(), 12)
            Worker('t').work(burst=True)  # files still being scanned: each job re-queues itself for later
            self.assertTrue(all(not r.sha256 for r in self.rows(batch).values()))
            self.assertEqual(Job.objects.filter(name='documents.file_facts', status__in=Job.READY).count(), 12)
            clean['on'] = True
            Job.objects.filter(status__in=Job.READY).update(run_after=Job.objects.first().created_at)
            Worker('t').work(burst=True)
            rows = self.rows(batch)
            self.assertTrue(all(r.processed and len(r.sha256) == 64 and r.page_count == 3 for r in rows.values()))
