"""
Regression tests for the independent review of the handover pack (one class per finding).

  1 an Excel error or text in the summary total, or no saved values at all, must not let the pack be issued
  2 issuing and uploading can't cross: the project lock, and an issued pack is built from its issued revision
  3 "to the penny" compares the unrounded totals (rounding each side first flagged good workbooks)
  4 shared strings and styles caps follow [Content_Types].xml, so renaming the part doesn't dodge them
  5 open item owners must be able to open the pack (estimators lose a won job; delivery staff only)
  6 upload races and absurd totals give a message, never a 500 or a DataError
  7 a workbook clarification removed in Siteline stays removed after the next upload
"""
import io
import zipfile
from decimal import Decimal as D
from unittest import mock

from django.db import IntegrityError
from django.test import SimpleTestCase

from core import ooxml
from handoverpack import engine, mapping, reader, sample, services
from handoverpack.access import possible_owners
from handoverpack.models import HandoverPack, OpenItem, PackClarification, PricingSettings, PricingWorkbook
from levelling import importer
from projects.models import Project, ProjectMember
from tenancy.context import org_context

from .test_engine import run, small
from .test_views import Base, demo_bytes, upload_file

ERROR_TOTAL = {('PRICING SUMMARY', 'G50'): '#REF!', ('Electrical', 'AA9'): D('1.00')}


class ErrorOrMissingTotalTests(Base):
    """1. A total Excel couldn't work out (#REF!, text) is cached-but-wrong; no saved values blocks by default."""

    def test_engine_treats_an_error_total_as_not_reconciled(self):
        for bad in ('#REF!', '228,000'):
            with self.subTest(bad):
                r = run(sample.build(sample.DEMO, stale={('PRICING SUMMARY', 'G50'): bad}))
                self.assertTrue(r['cached'])
                self.assertIs(r['totals']['reconciled'], False)
                self.assertIn(('PRICING SUMMARY', 'G50'), [(i['sheet'], i['cell']) for i in r['issues']])

    def test_an_error_total_cant_be_issued_and_is_an_open_item(self):
        wb = self.seed(self.pa, self.pat, sample.build(sample.DEMO, stale=ERROR_TOTAL))
        self.assertIs(wb.reconciled, False)
        self.login(self.pat)
        page = self.client.get(self.url('hpack_pack', self.pa.pk))
        self.assertContains(page, 'disabled aria-disabled="true">Issue to the delivery team')
        self.client.post(self.url('hpack_issue', self.pa.pk))
        with org_context(self.org_a):
            pack = HandoverPack.objects.get(project=self.pa)
            self.assertFalse(pack.is_issued)
            self.assertTrue(OpenItem.objects.filter(pack=pack, key='rec:total', status=OpenItem.OPEN).exists())
            self.assertTrue(OpenItem.objects.filter(pack=pack, key='rec:Electrical!AA9').exists())

    def test_no_saved_values_blocks_issue_unless_the_company_allows_it(self):
        self.seed(self.pa, self.pat, demo_bytes('C', cached=False))
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('hpack_pack', self.pa.pk)), 'disabled aria-disabled')
        self.client.post(self.url('hpack_issue', self.pa.pk))
        with org_context(self.org_a):
            self.assertFalse(HandoverPack.objects.get(project=self.pa).is_issued)
            PricingSettings.objects.create(organization=self.org_a, unchecked_issue=PricingSettings.WARN)
        self.assertNotContains(self.client.get(self.url('hpack_pack', self.pa.pk)), 'disabled aria-disabled')
        self.client.post(self.url('hpack_issue', self.pa.pk))
        with org_context(self.org_a):
            self.assertTrue(HandoverPack.objects.get(project=self.pa).is_issued)


class IssueUploadRaceTests(Base):
    """2. An upload that finishes after the pack was issued is refused; the pack shows what was issued."""

    def test_issue_during_an_upload(self):
        self.seed(self.pa, self.pat)
        self.login(self.pat)
        real = services.read

        def slow_read(data, org):
            out = real(data, org)
            with org_context(self.org_a):  # meanwhile, someone issues the pack
                services.issue(HandoverPack.objects.get(project=self.pa), self.quinn)
            return out
        with mock.patch.object(services, 'read', slow_read):
            r = self.client.post(self.url('hpack_upload', self.pa.pk), {'file': upload_file(demo_bytes('B'))},
                                 follow=True)
        self.assertContains(r, 'has been issued')
        with org_context(self.org_a):
            pack = HandoverPack.objects.get(project=self.pa)
            self.assertTrue(pack.is_issued)
            self.assertEqual(PricingWorkbook.objects.filter(project=self.pa).count(), 1)
            self.assertEqual(pack.workbook_id, pack.issued_workbook_id)

    def test_issued_pack_is_built_from_its_issued_revision(self):
        first = self.seed(self.pa, self.pat)
        with org_context(self.org_a):
            pack = services.issue(HandoverPack.objects.get(project=self.pa), self.pat)
            # A later revision row exists (as an import or admin might leave it); the issued pack ignores it
            later = PricingWorkbook.objects.create(
                project=self.pa, revision=2, original_name='x.xlsx', sha256='0' * 64, mapping_key='standard',
                cost=D('1'), charge=D('1'), profit=D('0'), reconciled=True, data=first.data, label='LATER')
            HandoverPack.objects.filter(pk=pack.pk).update(workbook=later)
        self.login(self.pat)
        page = self.client.get(self.url('hpack_pack', self.pa.pk))
        self.assertContains(page, f'revision {first.revision}')
        self.assertContains(page, '(as issued)')
        self.assertNotContains(page, 'workbook revision LATER')


class PennyToleranceTests(SimpleTestCase):
    """3. Totals half a penny apart agree; rounding each side first used to call them a penny out."""

    def spec(self):
        return small(lines=[sample.line(f'E{i}', f'Item {i}', 1, 'nr', (D('0.005'), None, '')) for i in range(3)])

    def test_rounding_boundary_is_not_a_penny_out(self):
        # Recalculated 0.0165 (rounds to 0.02); saved 0.0145 (rounds to 0.01): 0.002 apart
        r = run(sample.build(self.spec(), stale={('PRICING SUMMARY', 'G50'): D('0.0145')}))
        self.assertTrue(r['totals']['reconciled'])

    def test_more_than_half_a_penny_is_out(self):
        r = run(sample.build(self.spec(), stale={('PRICING SUMMARY', 'G50'): D('0.0105')}))
        self.assertIs(r['totals']['reconciled'], False)


def _with_renamed_shared_strings(base, name, n=None, extra_types=b''):
    zin = zipfile.ZipFile(io.BytesIO(base))
    ss = (b'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/'
          b'main">' + b''.join(b'<si><t>s%d</t></si>' % i for i in range(n or 10)) + b'</sst>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for i in zin.infolist():
            data = zin.read(i)
            if i.filename == '[Content_Types].xml':
                data = data.replace(b'</Types>', b'<Override PartName="/' + name.encode() + b'" ContentType="application/'
                                    b'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                                    + extra_types + b'</Types>')
            z.writestr(i, data)
        z.writestr(name, ss)
    return out.getvalue()


class RenamedPartTests(SimpleTestCase):
    """4. The caps follow the content type, whatever the part is called; levelling uses the same check."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = sample.build(small())

    def test_too_many_strings_in_a_renamed_part(self):
        data = _with_renamed_shared_strings(self.base, 'xl/notes.xml', n=ooxml.MAX_SHARED_STRINGS + 10)
        with self.assertRaises(importer.WorkbookRefused):
            importer._check_zip(data)
        m = mapping.get('standard')
        with self.assertRaises(reader.WorkbookRefused):
            reader.open_book(data, engine.extents(m))

    def test_size_cap_applies_to_a_renamed_part(self):
        data = _with_renamed_shared_strings(self.base, 'xl/notes.xml', n=2000)
        importer._check_zip(data)  # small: fine
        with mock.patch.object(ooxml, 'MAX_WHOLE', 1000):
            with self.assertRaises(importer.WorkbookRefused):
                importer._check_zip(data)

    def test_a_default_rule_for_shared_strings_is_refused(self):
        data = _with_renamed_shared_strings(self.base, 'xl/notes.xml', extra_types=(
            b'<Default Extension="bin" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.'
            b'sharedStrings+xml"/>'))
        with self.assertRaises(importer.WorkbookRefused):
            importer._check_zip(data)


class OwnerTests(Base):
    """5. Owners must be able to open the pack."""

    def test_estimator_owners_are_moved_to_the_pm_at_the_win(self):
        self.seed(self.bid, self.eve)
        with org_context(self.org_a):
            OpenItem.objects.filter(project=self.bid).update(owner=self.eve)
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_WON, manager=self.pat)
            self.bid.refresh_from_db()
            services.release(self.bid, self.alice)
            owners = set(OpenItem.objects.filter(project=self.bid, status=OpenItem.OPEN).values_list('owner', flat=True))
        self.assertEqual(owners, {self.pat.pk})

    def test_estimating_only_staff_are_not_offered_on_a_won_job(self):
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.eve, role=ProjectMember.ROLE_QS)
            people = set(possible_owners(self.pa))
        self.assertNotIn(self.eve, people)
        self.assertIn(self.quinn, people)
        self.assertIn(self.pat, people)
        self.assertIn(self.adam, people)
        self.assertNotIn(self.stan, people)


class UploadFailureTests(Base):
    """6. A lost race is a message, not a 500, and leaves no file behind; absurd totals are refused."""

    def test_revision_clash_is_a_message(self):
        from django.core.files.storage import default_storage
        from django.db.models.fields.files import FieldFile
        self.login(self.pat)
        saved, real_save = [], FieldFile.save

        def record(fieldfile, name, content, save=True):
            real_save(fieldfile, name, content, save=save)
            saved.append(fieldfile.name)
        with mock.patch.object(FieldFile, 'save', record), \
                mock.patch.object(PricingWorkbook, 'save', side_effect=IntegrityError('duplicate revision')):
            r = self.client.post(self.url('hpack_upload', self.pa.pk), {'file': upload_file()}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'at the same moment')
        with org_context(self.org_a):
            self.assertFalse(PricingWorkbook.objects.exists())
        self.assertTrue(saved)
        for name in saved:  # the file written before the row failed was removed again
            self.assertFalse(default_storage.exists(name), name)

    def test_absurd_totals_are_refused(self):
        spec = small(lines=[sample.line('E1', 'Typo', D('1e13'), 'nr', (D('10'), None, ''))])
        self.login(self.pat)
        r = self.client.post(self.url('hpack_upload', self.pa.pk), {'file': upload_file(sample.build(spec))},
                             follow=True)
        self.assertContains(r, 'far too large')
        with org_context(self.org_a):
            self.assertFalse(PricingWorkbook.objects.exists())


class HiddenClarificationTests(Base):
    """7. Removing a workbook clarification hides it, and the next upload leaves it hidden."""

    def test_removed_workbook_row_stays_removed(self):
        self.seed(self.bid, self.eve)
        with org_context(self.org_a):
            c = PackClarification.objects.get(pack__project=self.bid, ref='CL1')
        self.login(self.eve)
        self.client.post(self.url('hpack_clarification', self.bid.pk, c.pk), {'delete': '1'})
        self.seed(self.bid, self.eve)
        with org_context(self.org_a):
            pack = HandoverPack.objects.get(project=self.bid)
            self.assertEqual(PackClarification.objects.filter(pack=pack, ref='CL1').count(), 1)
            self.assertFalse(pack.visible_clarifications().filter(ref='CL1').exists())
        page = self.client.get(self.url('hpack_clarifications', self.bid.pk))
        self.assertNotContains(page, 'Works carried out during normal working hours')
        self.assertEqual(self.client.get(self.url('hpack_clarification', self.bid.pk, c.pk)).status_code, 404)
