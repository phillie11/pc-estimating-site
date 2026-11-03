"""
Review of claude/r3-cvr: one test per defect found, each failing before its fix.

  1. A daywork sheet is signed (and later priced on the change register) at the percentage additions
     stored when a line was last touched, not the job's current ones it showed while being written.
  2. A line or photo can land on a sheet signed a moment earlier: the stale-copy retry in _touch saves
     regardless, so the client's signed figures change after signing.
  3. The cashflow's money in doesn't tie to the forecast final account: retention is taken off each
     month's share and rounded on its own, so pennies are lost; and once practical completion is past,
     the completion half (and with a nil defects period, all) of the retention is never forecast.
  4. A signed-off CVR can still be changed: refresh_from_db() leaves the lock looking at the old status,
     and queryset.update() goes straight past save().
  5. The company CVR at /cvr/ opens for any staff member, estimators included (an empty page, not a refusal).
  6. A signature picture of 40 million blank pixels (170 KB as a PNG) is decoded and copied several
     times over (about 600 MB) before it is refused or accepted.
  7. Certified to date for a past month counts payment notices given, and applications that only
     stood as notified, after that month's end.
  8. actions.providers.JOB_ANNOTATIONS treats a broken import inside an app as "not merged yet" and
     silently drops its flags (and lets two apps overwrite each other's flags).
"""
import io
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from core.workflow import apply_transition
from cvr import cashflow, figures, rules, services
from cvr.forms import SignatureForm
from cvr.models import CVRPeriod, DayworkLine, DayworkSheet
from payments.models import Application
from projects.models import Project
from tenancy.context import org_context

from .base import CVRTestBase
from .test_views import png


class DayworkSigningUsesTheJobsCurrentAdditionsTests(CVRTestBase):
    def test_signed_and_sent_at_the_percentages_shown_while_it_was_written(self):
        with org_context(self.org_a):
            terms = services.terms_for(self.pa, create=True)
            terms.labour_pct = D('10')
            terms.save()
            sheet = services.new_sheet(self.pa, self.stan, title='Move servery')
            DayworkLine.objects.create(organization_id=self.org_a.pk, sheet=sheet, kind=rules.LABOUR,
                                       description='Joiner', quantity=D('10'), unit_rate=D('40'))
            # The PM agrees the contract's labour addition is 20%. "Sheets not yet signed follow them."
            terms.labour_pct = D('20')
            terms.save()
        self.login(self.stan)
        page = self.client.get(self.url('daywork_detail', self.pa.pk, sheet.pk))
        self.assertContains(page, '£480.00')  # 400 prime + 20% shown while it is written
        r = self.client.post(self.url('daywork_sign', self.pa.pk, sheet.pk), {
            'name': 'Client Rep', 'role': 'CLIENT', 'confirm': 'on',
            'signature': SimpleUploadedFile('signature.png', png(), content_type='image/png')})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            sheet = DayworkSheet.objects.get(pk=sheet.pk)
            self.assertEqual(sheet.status, DayworkSheet.SIGNED)
            self.assertEqual(sheet.labour_pct, D('20.00'))
            ce, _ = services.send_to_change(sheet, self.pat)
            revenue = ce.lines.get(kind='REVENUE').amount
        self.assertEqual(revenue, D('480.00'))


@override_settings(MAX_UPLOAD_MB=5)
class NoLinesAfterSigningTests(CVRTestBase):
    def test_a_line_posted_from_a_page_opened_before_signing_is_refused(self):
        with org_context(self.org_a):
            sheet = services.new_sheet(self.pa, self.stan, title='Move servery')
            DayworkLine.objects.create(organization_id=self.org_a.pk, sheet=sheet, kind=rules.LABOUR,
                                       description='Joiner', quantity=D('8'), unit_rate=D('40'))
            stale = DayworkSheet.objects.select_related('created_by', 'change_event').get(pk=sheet.pk)
            signed = DayworkSheet.objects.get(pk=sheet.pk)
            apply_transition(signed, 'sign', self.stan, signer='Client Rep', role='CLIENT',
                             signature=SimpleUploadedFile('s.png', png(), content_type='image/png'))
        self.login(self.stan)
        # The request read the sheet just before the signature was saved
        with mock.patch('cvr.views._sheet', return_value=stale):
            self.client.post(self.url('daywork_detail', self.pa.pk, sheet.pk), {
                'action': 'add_line', 'line-kind': 'LABOUR', 'line-description': 'Extra joiner',
                'line-quantity': '20', 'line-unit_rate': '40'})
        with org_context(self.org_a):
            self.assertEqual(DayworkLine.objects.filter(sheet=sheet).count(), 1)
            self.assertEqual(DayworkSheet.objects.get(pk=sheet.pk).price()['prime'], D('320.00'))

    def test_the_model_refuses_lines_on_a_signed_sheet(self):
        with org_context(self.org_a):
            sheet = services.new_sheet(self.pa, self.stan, title='Move servery')
            line = DayworkLine.objects.create(organization_id=self.org_a.pk, sheet=sheet, kind=rules.LABOUR,
                                              description='Joiner', quantity=D('8'), unit_rate=D('40'))
            apply_transition(sheet, 'sign', self.stan, signer='Client Rep', role='CLIENT',
                             signature=SimpleUploadedFile('s.png', png(), content_type='image/png'))
            with self.assertRaises(PermissionError):
                DayworkLine.objects.create(organization_id=self.org_a.pk, sheet=sheet, kind=rules.LABOUR,
                                           description='Late', quantity=D('1'), unit_rate=D('1'))
            with self.assertRaises(PermissionError):
                DayworkLine.objects.get(pk=line.pk).delete()


class CashflowTiesToThePennyTests(CVRTestBase):
    def test_money_in_adds_up_to_the_final_account_with_retention_pennies(self):
        self.build_money()
        with org_context(self.org_a):
            # Three months of work left (this month and the next two) and 1,000.00 of value to come:
            # 333.33, 333.33 and 333.34 less 5% retention each round to 949.99, not 950.00
            self.pa.value = D('51000.00')
            self.pa.completion_date = rules.add_months(self.m0, 2).replace(day=20)
            self.pa.save()
            data = cashflow.build(Project.objects.get(pk=self.pa.pk), self.today)
        self.assertEqual(data['remaining_value'], D('1000.00'))
        self.assertEqual(data['totals']['in'], data['final_account'])

    def test_retention_comes_back_when_completion_is_past_and_the_defects_period_is_nil(self):
        from contracts.models import MainContract
        with org_context(self.org_a):
            done = Project.objects.create(name='Finished job', stage=Project.STAGE_LIVE, value=D('123456.78'),
                                          manager=self.pat, completion_date=self.today - timedelta(days=40))
            contract = MainContract(project=done, form='JCT_IC', retention_percent=D('5'), valuation_day=25,
                                    defects_period_months=0, practical_completion_on=self.today - timedelta(days=40))
            contract.apply_preset(only_blank=True, creating=True)
            contract.save()
            data = cashflow.build(Project.objects.get(pk=done.pk), self.today)
        # Both halves of the retention were skipped (the defects end fell on the completion date, already past)
        self.assertEqual(data['totals']['in'], D('123456.78'))


class SignedOffIsLockedTests(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.build_money()
        with org_context(self.org_a):
            self.period, _ = services.start_period(self.pa, self.m1, self.pat, self.today)
            apply_transition(self.period, 'submit', self.pat, today=self.today)

    def sign_off_elsewhere(self):
        with org_context(self.org_a):
            other = CVRPeriod.objects.get(pk=self.period.pk)
            apply_transition(other, 'sign_off', self.alice)

    def test_a_copy_refreshed_after_sign_off_cant_be_saved(self):
        with org_context(self.org_a):
            mine = CVRPeriod.objects.get(pk=self.period.pk)
        self.sign_off_elsewhere()
        with org_context(self.org_a):
            mine.refresh_from_db()
            mine.commentary = 'Changed after sign-off'
            with self.assertRaises(PermissionError):
                mine.save(actor=self.alice)
            self.assertEqual(CVRPeriod.objects.get(pk=self.period.pk).commentary, '')

    def test_a_queryset_update_cant_change_a_signed_off_month(self):
        self.sign_off_elsewhere()
        with org_context(self.org_a):
            with self.assertRaises(PermissionError):
                CVRPeriod.objects.filter(pk=self.period.pk).update(cost_adjustments=D('99999'))
            with self.assertRaises(PermissionError):
                CVRPeriod.all_objects.filter(pk=self.period.pk).update(status=CVRPeriod.DRAFT)
            self.assertEqual(CVRPeriod.objects.get(pk=self.period.pk).status, CVRPeriod.SIGNED_OFF)
            # Months still open can be updated in bulk as before
            p0, _ = services.start_period(self.pa, self.m0, self.pat, self.today)
            self.assertEqual(CVRPeriod.objects.filter(pk=p0.pk).update(commentary='ok'), 1)


class CompanyCVRRefusesStaffWithNoJobsTests(CVRTestBase):
    def test_estimators_and_site_staff_are_refused(self):
        self.build_money()
        url = self.url('company_cvr')
        self.assertEqual(self.status_for(self.eve, url), 403)
        self.assertEqual(self.status_for(self.stan, url), 403)
        self.assertEqual(self.status_for(self.eve, url + '?export=csv'), 403)
        self.assertEqual(self.status_for(self.pat, url), 200)
        self.assertEqual(self.status_for(self.adam, url), 200)


class SignatureSizeTests(CVRTestBase):
    def test_a_huge_blank_signature_is_refused_before_it_is_decoded(self):
        from PIL import Image
        img = Image.new('RGBA', (6300, 6300), (255, 255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        del img
        upload = SimpleUploadedFile('signature.png', buf.getvalue(), content_type='image/png')
        with mock.patch('PIL.ImageFile.ImageFile.load', side_effect=AssertionError('decoded a huge picture')):
            form = SignatureForm({'name': 'Client Rep', 'role': 'CLIENT', 'confirm': 'on'}, {'signature': upload})
            self.assertFalse(form.is_valid())
        self.assertIn('too large', str(form.errors['signature']))

    def test_a_phone_photo_of_a_paper_sheet_still_signs(self):
        from PIL import Image, ImageDraw
        img = Image.new('RGB', (4032, 3024), (250, 250, 245))
        ImageDraw.Draw(img).line((200, 2000, 1800, 1500, 3000, 2100), fill=(10, 10, 10), width=12)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=70)
        form = SignatureForm({'name': 'Client Rep', 'role': 'CLIENT', 'confirm': 'on'},
                             {'signature': SimpleUploadedFile('sheet.jpg', buf.getvalue(), content_type='image/jpeg')})
        self.assertTrue(form.is_valid(), form.errors)


class CertifiedAsAtTheMonthEndTests(CVRTestBase):
    def test_a_notice_given_after_the_month_end_doesnt_count_in_that_month(self):
        rows = self.build_money()
        with org_context(self.org_a):
            ap2 = Application.objects.get(pk=rows['ap2'].pk)
            # AP-002 (valued on the 25th of last month) only stood as notified, or got its payment notice,
            # this month: at last month's end nothing on it was certified
            Application.objects.filter(pk=ap2.pk).update(
                payment_notice_by=self.day(self.m0, 1), payment_notice_on=None)
            before = figures.live_figures([self.pa], self.m1)[self.pa.pk]
            ap1 = rows['ap1']
            ap1.refresh_from_db()
        self.assertEqual(before.certified_to_date, ap1.notified_sum)
        with org_context(self.org_a):
            Application.objects.filter(pk=ap2.pk).update(
                status=Application.STATUS_NOTIFIED, payment_notice_on=self.day(self.m0, 1),
                payment_notice_amount=D('30000'), payment_notice_by=self.day(self.m0, 1) + timedelta(days=3))
            again = figures.live_figures([self.pa], self.m1)[self.pa.pk]
            now = figures.live_figures([self.pa], self.m0)[self.pa.pk]
        self.assertEqual(again.certified_to_date, ap1.notified_sum)
        self.assertEqual(now.certified_to_date, ap1.notified_sum + D('30000.00'))


class CheckedSoundTests(CVRTestBase):
    """Not defects: what the review tried and found held."""

    def test_sending_a_sheet_twice_makes_one_change_event(self):
        from changes.models import ChangeEvent
        with org_context(self.org_a):
            sheet = services.new_sheet(self.pa, self.stan, title='Move servery')
            DayworkLine.objects.create(organization_id=self.org_a.pk, sheet=sheet, kind=rules.LABOUR,
                                       description='Joiner', quantity=D('8'), unit_rate=D('40'))
            stale = DayworkSheet.objects.select_related('created_by', 'change_event').get(pk=sheet.pk)
        self.login(self.stan)
        url = self.url('daywork_detail', self.pa.pk, sheet.pk)
        self.client.post(url, {'action': 'send', 'send-change_event': ''})
        with mock.patch('cvr.views._sheet', return_value=stale):  # the second click read it before the first
            self.client.post(url, {'action': 'send', 'send-change_event': ''})
        with org_context(self.org_a):
            self.assertEqual(ChangeEvent.objects.filter(project=self.pa).count(), 1)

    def test_an_adjustment_from_a_page_read_before_sign_off_is_refused(self):
        self.build_money()
        with org_context(self.org_a):
            period, _ = services.start_period(self.pa, self.m1, self.pat, self.today)
            stale = CVRPeriod.objects.get(pk=period.pk)
            apply_transition(period, 'submit', self.pat, today=self.today)
            apply_transition(CVRPeriod.objects.get(pk=period.pk), 'sign_off', self.alice)
            with self.assertRaises(Exception):
                services.add_adjustment(stale, 'COST', D('5000'), 'Late accrual', self.pat)
            self.assertEqual(period.adjustments.count(), 0)
            self.assertEqual(CVRPeriod.objects.get(pk=period.pk).status, CVRPeriod.SIGNED_OFF)


class JobAnnotationsHookTests(CVRTestBase):
    """8. actions.providers.JOB_ANNOTATIONS swallowed any ModuleNotFoundError, hiding a broken import."""

    def test_a_missing_app_is_skipped_but_a_broken_one_is_not(self):
        from actions import providers
        request = self.request_for(self.pat)
        with mock.patch.object(providers, 'JOB_ANNOTATIONS', ['notmergedyet.actions.job_annotations']):
            self.assertEqual(providers._job_annotations(request), {})
        inner = ModuleNotFoundError("No module named 'reportlib'", name='reportlib')
        with mock.patch.object(providers, 'JOB_ANNOTATIONS', ['cvr.actions.job_annotations']), \
                mock.patch('importlib.import_module', side_effect=inner):
            with self.assertRaises(ModuleNotFoundError):
                providers._job_annotations(request)
