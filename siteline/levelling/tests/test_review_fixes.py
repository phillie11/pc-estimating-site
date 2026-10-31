"""
Follow-ups to the independent review of quote levelling (F1-F9 and the read-only access for the
job's project manager), beyond the reproductions in test_review_repro.py.
"""
import io
import zipfile
from decimal import Decimal as D

from django.contrib.messages import get_messages
from django.test import SimpleTestCase

from directory.models import Package, Trade
from projects.models import Project, ProjectMember
from tenancy.context import org_context
from tenancy.models import ActivityEvent

from levelling import importer, services
from levelling.importer import WorkbookRefused
from levelling.models import Adjustment, LevellingSheet, QuoteCell, ScopeLine

from .test_import import HEAD, csv_file, xlsx
from .test_review_repro import BAD_DATE, crafted
from .test_views import LevellingBase


def msgs(r):
    return ' '.join(str(m) for m in get_messages(r.wsgi_request))


class TradeDeleteTests(LevellingBase):
    def test_admin_is_told_why_a_levelled_trade_stays(self):
        self.login(self.alice)
        r = self.client.post(self.url('trades', self.org_a), {'action': 'delete', 'trade': self.join.pk})
        self.assertIn("Joinery has quote levelling on 1 bid, so it can't be deleted.", msgs(r))
        with org_context(self.org_a):
            self.assertTrue(Trade.objects.filter(pk=self.join.pk).exists())
            spare = Trade.objects.create(name='Spare trade')
        self.client.post(self.url('trades', self.org_a), {'action': 'delete', 'trade': spare.pk})
        with org_context(self.org_a):
            self.assertFalse(Trade.objects.filter(pk=spare.pk).exists())

    def test_staff_cannot_delete_trades(self):
        with org_context(self.org_a):
            spare = Trade.objects.create(name='Spare trade')
        for user in (self.est, self.pm):
            self.login(user)
            r = self.client.post(self.url('trades', self.org_a), {'action': 'delete', 'trade': spare.pk})
            self.assertEqual(r.status_code, 403)
            self.assertNotContains(self.client.get(self.url('trades', self.org_a)), 'value="delete"')
        with org_context(self.org_a):
            self.assertTrue(Trade.objects.filter(pk=spare.pk).exists())


class PackageRemoveTests(LevellingBase):
    def test_levelled_package_is_kept_even_while_tendering(self):
        with org_context(self.org_a):
            Package.objects.filter(pk=self.p_tha.pk).update(status=Package.UNSUCCESSFUL)
        self.login(self.est)
        r = self.client.post(self.url('package_list', self.org_a, self.bid.pk), {'action': 'remove', 'package': self.p_tha.pk})
        self.assertIn('is in the levelling for this trade', msgs(r))
        with org_context(self.org_a):
            self.assertTrue(Package.objects.filter(pk=self.p_tha.pk).exists())

    def test_unlevelled_declined_package_can_still_be_removed_while_tendering(self):
        with org_context(self.org_a):
            Package.objects.filter(pk=self.p_lund.pk).update(status=Package.DECLINED)
        self.login(self.est)
        self.client.post(self.url('package_list', self.org_a, self.bid.pk), {'action': 'remove', 'package': self.p_lund.pk})
        with org_context(self.org_a):
            self.assertFalse(Package.objects.filter(pk=self.p_lund.pk).exists())

    def test_database_backstop(self):
        from django.db.models import RestrictedError
        with org_context(self.org_a), self.assertRaises(RestrictedError):
            Package.objects.get(pk=self.p_oak.pk).delete()


class StalePlugTests(LevellingBase):
    def test_import_of_a_priced_line_removes_the_plug_and_logs_it(self):
        self.post({'action': 'plug', 'line': self.l6.pk, 'package': self.p_ken.pk, 'basis': 'AVERAGE'})
        self.assertEqual(self.view().column(self.p_ken.pk).levelled, D('39989.00'))
        r = self.post({'imp-kind': 'priced', 'imp-package': self.p_ken.pk,
                       'imp-file': csv_file('Ref,Description,Qty,Unit,Rate\nJ6,Window boards,42,m,40\n')},
                      url=self.url('levelling_import', self.org_a, self.bid.pk, self.join.pk))
        self.assertIn('1 plug removed', msgs(r))
        with org_context(self.org_a):
            self.assertFalse(Adjustment.objects.filter(package=self.p_ken, kind=Adjustment.PLUG).exists())
            self.assertTrue(ActivityEvent.objects.filter(verb='removed plug, line now priced').exists())
        # Quoted total is their tender sum: 38,330, nothing added
        self.assertEqual(self.view().column(self.p_ken.pk).levelled, D('38330.00'))

    def test_rates_page_removes_the_plug(self):
        self.post({'action': 'plug', 'line': self.l6.pk, 'package': self.p_ken.pk, 'basis': 'HIGHEST'})
        url = self.url('levelling_rates', self.org_a, self.bid.pk, self.join.pk, self.p_ken.pk)
        self.post({f'rate_{self.l5.pk}': '655', f'rate_{self.l6.pk}': '40', f'incl_{self.l7.pk}': '1'}, url=url)
        with org_context(self.org_a):
            self.assertFalse(Adjustment.objects.filter(package=self.p_ken).exists())


class ImporterHardeningTests(SimpleTestCase):
    def _zip(self, parts):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, data in parts.items():
                z.writestr(name, data)
        return buf.getvalue()

    def test_doctype_and_entities_refused(self):
        f = xlsx([HEAD, ['J1', 'Desk', 1, 'nr']])
        src = zipfile.ZipFile(io.BytesIO(f.read()))
        parts = {i.filename: src.read(i) for i in src.infolist()}
        body = parts['xl/worksheets/sheet1.xml']
        if body.startswith(b'<?xml'):
            body = body.split(b'?>', 1)[1]
        parts['xl/worksheets/sheet1.xml'] = (b'<?xml version="1.0"?><!DOCTYPE w [<!ENTITY x SYSTEM '
                                             b'"file:///etc/passwd">]>' + body)
        from django.core.files.uploadedfile import SimpleUploadedFile
        with self.assertRaisesMessage(WorkbookRefused, 'document type declaration'):
            importer.read(SimpleUploadedFile('x.xlsx', self._zip(parts)))

    def test_shared_strings_count_and_ratio_limits(self):
        f = crafted('', shared='<si><t>a</t></si>' * 60000)
        with self.assertRaises(WorkbookRefused):
            importer.read(f)
        # A sheet part that unpacks to more than 100x its packed size
        f = crafted('<row r="2"><c r="A2" t="inlineStr"><is><t>J1</t></is></c></row>' + ' ' * (2 * 1024 * 1024))
        with self.assertRaisesMessage(WorkbookRefused, 'too large once unpacked'):
            importer.read(f)

    def test_crafted_cells_are_refused_not_raised(self):
        with self.assertRaisesMessage(WorkbookRefused, "couldn't be read"):
            importer.read(crafted(BAD_DATE))

    def test_bounds_and_nuls(self):
        self.assertIsNone(importer.parse_qty('100000000000'))
        self.assertEqual(importer.parse_qty('99999999999.9994'), D('99999999999.999'))
        self.assertIsNone(importer.parse_money('999999999999.995'))   # rounds to 10^12
        self.assertEqual(importer.parse_money('999999999999.994'), D('999999999999.99'))
        rows, _ = importer.read(csv_file('Ref,Description,Qty,Unit\nX1,a\x00b,500000000000,m\n'))
        self.assertEqual((rows[0].description, rows[0].qty), ('ab', None))


class SizeLimitTests(LevellingBase):
    def test_plug_too_large_is_refused_with_a_message(self):
        with org_context(self.org_a):
            big = services.add_line(self.sheet, 'X9', 'Big', D('1000'), 'm')
            services.set_cell(big, self.p_oak, rate=D('50000000000'))
        r = self.post({'action': 'plug', 'line': big.pk, 'package': self.p_ken.pk, 'basis': 'HIGHEST'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('too large to record', msgs(r))
        with org_context(self.org_a):
            self.assertFalse(Adjustment.objects.filter(line=big).exists())


class ZeroLowestTests(LevellingBase):
    def test_pdf_and_page_show_a_dash(self):
        with org_context(self.org_a):
            Adjustment.objects.create(sheet=self.sheet, package=self.p_ken, kind=Adjustment.CORRECTION,
                                      amount=D('-38330'), reason='zero it')
        v = self.view()
        self.assertIsNone(v.column(self.p_tha.pk).result.var_lowest_pct)
        from levelling.exports import pct_or_dash
        self.assertEqual(pct_or_dash(None), '—')
        self.login(self.est)
        self.assertEqual(self.client.get(self.url('levelling_pdf', self.org_a, self.bid.pk, self.join.pk)).status_code, 200)


class ProjectManagerReadOnlyTests(LevellingBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_WON, manager=self.pm)

    def test_job_pm_reads_but_cannot_change(self):
        self.login(self.pm)
        for url in (self.url('levelling_index', self.org_a, self.bid.pk), self.sheet_url(),
                    self.url('levelling_rates', self.org_a, self.bid.pk, self.join.pk, self.p_ken.pk),
                    self.url('levelling_csv', self.org_a, self.bid.pk, self.join.pk),
                    self.url('levelling_pdf', self.org_a, self.bid.pk, self.join.pk)):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get(self.sheet_url())
        self.assertContains(r, 'read only')
        self.assertNotContains(r, 'Add line')
        for url, data in ((self.sheet_url(), {'action': 'uncarry'}),
                          (self.sheet_url(), {'action': 'add_line', 'line-description': 'x'}),
                          (self.url('levelling_rates', self.org_a, self.bid.pk, self.join.pk, self.p_ken.pk),
                           {f'rate_{self.l6.pk}': '1'}),
                          (self.url('levelling_import', self.org_a, self.bid.pk, self.join.pk),
                           {'imp-kind': 'scope', 'imp-file': csv_file('J1,Desk,1,nr\n')})):
            self.assertEqual(self.client.post(url, data).status_code, 403, url)
        with org_context(self.org_a):
            self.assertEqual(ScopeLine.objects.filter(sheet=self.sheet).count(), 3)
            self.assertFalse(QuoteCell.objects.filter(line=self.l6, package=self.p_ken).exists())

    def test_links_from_packages_tab_and_handover(self):
        self.login(self.pm)
        self.assertContains(self.client.get(self.url('package_list', self.org_a, self.bid.pk)), 'Tender levelling')

    def test_other_projects_staff_are_refused(self):
        from tenancy.models import Membership
        from tenancy.tests import make_user
        other = make_user('site@a.test', 'Sid Site')
        Membership.objects.create(organization=self.org_a, user=other, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        self.login(other)
        self.assertEqual(self.client.get(self.sheet_url()).status_code, 403)
        self.assertNotContains(self.client.get(self.url('package_list', self.org_a, self.bid.pk)), 'Tender levelling')
        # A QS on the job's team may read it
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.bid, user=other, role=ProjectMember.ROLE_QS)
        self.assertEqual(self.client.get(self.sheet_url()).status_code, 200)


class TenderPageLinkTests(LevellingBase):
    def test_not_levelled_hidden_from_staff_who_cannot_estimate(self):
        from tenancy.models import Membership
        with org_context(self.org_a):
            QuoteCell.objects.all().delete()
        self.login(self.est)
        self.assertContains(self.client.get(self.url('tender', self.org_a, self.bid.pk)), 'Not levelled')
        # Owners/admins and estimators only; the internal tender page shows nothing to anyone else
        m = Membership.objects.get(organization=self.org_a, user=self.est)
        m.area = Membership.AREA_DELIVERY
        m.save()
        with org_context(self.org_a):
            Project.objects.filter(pk=self.bid.pk).update(stage=Project.STAGE_WON)
        r = self.client.get(self.url('tender', self.org_a, self.bid.pk))
        self.assertNotContains(r, 'Not levelled')
        with org_context(self.org_a):
            self.assertEqual(LevellingSheet.objects.count(), 1)
