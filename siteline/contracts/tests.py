"""Contract set-up: presets, payment timetable, vocabulary, the views by role, and tenancy."""
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from changes.tests.base import CommercialTestBase
from projects.models import Project
from tenancy.context import TenantContextMissing, org_context

from . import presets
from .models import MainContract
from .vocabulary import vocabulary, vocabulary_for

D = date.fromisoformat


class PresetTableTests(SimpleTestCase):
    # form: (due after valuation, final after due, pay less before final, retention %, defects months, damages per)
    TABLE = {
        'JCT_MW': (0, 14, 5, Decimal('5'), 3, 'WEEK'),
        'JCT_MWD': (0, 14, 5, Decimal('5'), 3, 'WEEK'),
        'JCT_IC': (7, 14, 5, Decimal('5'), 6, 'WEEK'),   # 95% certified unless stated (product owner)
        'JCT_ICD': (7, 14, 5, Decimal('5'), 6, 'WEEK'),
        'JCT_SBC': (7, 14, 5, Decimal('3'), 6, 'WEEK'),
        'JCT_DB': (7, 14, 5, Decimal('3'), 6, 'WEEK'),
        'NEC4_ECC': (7, 14, 7, None, None, 'DAY'),
        'BESPOKE': (7, 17, 7, None, None, 'WEEK'),  # the Scheme: interim due 7 days after the period (para 4)
    }

    def test_every_form_has_a_preset(self):
        self.assertEqual(set(self.TABLE), {code for code, _ in MainContract.FORM_CHOICES})

    def test_table(self):
        for form, (due, final, pay_less, retention, defects, per) in self.TABLE.items():
            with self.subTest(form=form):
                p = presets.preset_for(form)
                self.assertEqual((p.due_date_offset_days, p.final_date_offset_days, p.pay_less_notice_days,
                                  p.retention_percent, p.defects_period_months, p.lads_period),
                                 (due, final, pay_less, retention, defects, per))
                self.assertEqual(p.retention_released_at_pc_percent, Decimal('50'))  # half at PC

    def test_scheme_interim_due_date_is_seven_days(self):
        # Regression: was 30, the Scheme's final payment rule (para 5), not the interim one (para 4)
        self.assertEqual(presets.SCHEME.due_date_offset_days, 7)
        self.assertIn('Part II para 4', presets.__doc__)

    def test_ic_retention_is_five_percent_and_confirmed(self):
        for form in ('JCT_IC', 'JCT_ICD'):
            self.assertEqual(presets.preset_for(form).retention_percent, Decimal('5'))
            self.assertNotIn('erify', presets.preset_for(form).note)
        self.assertEqual(presets.preset_for('JCT_SBC').retention_percent, Decimal('3'))

    def test_unknown_form_falls_back_to_the_scheme(self):
        self.assertIs(presets.preset_for('SOMETHING_ELSE'), presets.SCHEME)
        self.assertIn('Scheme', presets.SCHEME.label)

    def test_advisory_label(self):
        self.assertEqual(presets.ADVISORY, 'Advisory: check your contract.')
        self.assertIn('ADVISORY', presets.__doc__)


class PaymentTimetableTests(SimpleTestCase):
    # JCT IC: 7 / 14 / 5 days, counted leaving out Christmas Day, Good Friday and bank holidays (s116)
    # (valuation, region, due, final, pay less by)
    TABLE = [
        ('2026-11-28', 'england-and-wales', '2026-12-05', '2026-12-19', '2026-12-14'),   # nothing to skip
        # skips 25, Saturday 26 (a bank holiday by statute, 1971 Act Sch 1), 28 Dec and 1 Jan
        ('2026-12-21', 'england-and-wales', '2026-12-31', '2027-01-15', '2027-01-10'),
        # Scotland counts 26 Dec but skips Saturday 2 Jan (statute) and Monday 4 Jan (its substitute)
        ('2026-12-21', 'scotland', '2026-12-30', '2027-01-16', '2027-01-11'),
        ('2026-03-28', 'england-and-wales', '2026-04-05', '2026-04-20', '2026-04-15'),   # Good Friday, Easter Monday
        ('2026-03-28', 'scotland', '2026-04-05', '2026-04-19', '2026-04-14'),            # no Easter Monday
        ('2026-07-04', 'northern-ireland', '2026-07-11', '2026-07-26', '2026-07-21'),    # the Boyne (13 Jul)
        ('2026-07-04', 'england-and-wales', '2026-07-11', '2026-07-25', '2026-07-20'),
    ]

    def test_jct_ic_table(self):
        for valuation, region, due, final, pay_less in self.TABLE:
            with self.subTest(valuation=valuation, region=region):
                t = presets.payment_timetable(D(valuation), 7, 14, 5, region=region)
                self.assertEqual((t.due_date, t.final_date, t.pay_less_by), (D(due), D(final), D(pay_less)))

    def test_scheme(self):
        p = presets.SCHEME
        t = presets.payment_timetable(D('2026-01-31'), p.due_date_offset_days, p.final_date_offset_days,
                                      p.pay_less_notice_days)
        self.assertEqual((t.due_date, t.final_date, t.pay_less_by), (D('2026-02-07'), D('2026-02-24'), D('2026-02-17')))

    def test_contract_uses_its_region(self):
        c = MainContract(form='JCT_IC', valuation_day=21, bank_holiday_region='scotland')
        c.apply_preset()
        self.assertEqual(c.next_payment_timetable(D('2026-12-01')).final_date, D('2027-01-16'))

    def test_missing_terms_give_none(self):
        self.assertIsNone(presets.payment_timetable(None, 7, 14, 5))
        self.assertIsNone(presets.payment_timetable(D('2026-01-01'), 7, None, 5))

    def test_next_valuation_date(self):
        for today, day, expected in [('2026-01-10', 28, '2026-01-28'), ('2026-01-28', 28, '2026-01-28'),
                                     ('2026-01-29', 28, '2026-02-28'), ('2026-02-10', 31, '2026-02-28'),
                                     ('2028-02-10', 30, '2028-02-29'), ('2026-12-29', 15, '2027-01-15')]:
            with self.subTest(today=today, day=day):
                self.assertEqual(presets.next_valuation_date(D(today), day), D(expected))
        self.assertIsNone(presets.next_valuation_date(D('2026-01-01'), None))


class ContractModelTests(SimpleTestCase):
    def test_apply_preset_fills_only_blanks_unless_told(self):
        c = MainContract(form='JCT_IC', pay_less_notice_days=7)
        changed = c.apply_preset()
        self.assertEqual((c.due_date_offset_days, c.final_date_offset_days, c.pay_less_notice_days), (7, 14, 7))
        self.assertIn('retention_percent', changed)
        self.assertNotIn('pay_less_notice_days', changed)
        c.apply_preset(only_blank=False)
        self.assertEqual(c.pay_less_notice_days, 5)

    def test_preset_on_create_overrides_untouched_defaults(self):
        # Regression: NEC delay damages per day and the release share were never applied, as the model
        # defaults (per week, 50%) are not blank
        c = MainContract(form='NEC4_ECC', nec_main_option='A')
        self.assertNotIn('lads_period', c.apply_preset())
        self.assertEqual(c.lads_period, 'WEEK')
        c = MainContract(form='NEC4_ECC', nec_main_option='A')
        self.assertIn('lads_period', c.apply_preset(creating=True))
        self.assertEqual(c.lads_period, 'DAY')
        # A value the user changed from the default is still kept
        c = MainContract(form='NEC4_ECC', retention_released_at_pc_percent=Decimal('100'))
        c.apply_preset(creating=True)
        self.assertEqual(c.retention_released_at_pc_percent, Decimal('100'))

    def test_retention_rule_and_rounding(self):
        c = MainContract(form='JCT_IC', retention_percent=Decimal('3'))
        self.assertEqual(c.retention_rule, '3%: half released at Practical Completion, half at the end of the '
                                           'Rectification Period')
        self.assertEqual(c.retention_held(Decimal('100.50')), Decimal('3.02'))  # 3.015 rounds half up
        self.assertEqual(c.retention_held(Decimal('100.49')), Decimal('3.01'))
        nec = MainContract(form='NEC4_ECC', retention_percent=Decimal('5'), retention_released_at_pc_percent=Decimal('100'))
        self.assertEqual(nec.retention_rule, '5%: all released at Completion')
        self.assertEqual(MainContract(form='JCT_IC', retention_percent=Decimal('0')).retention_rule, 'No retention')

    def test_form_label(self):
        self.assertEqual(MainContract(form='JCT_IC', edition='2024').form_label,
                         'JCT Intermediate Building Contract (IC) 2024')
        self.assertEqual(MainContract(form='NEC4_ECC', nec_main_option='A').form_label,
                         'NEC4 Engineering and Construction Contract (ECC), Option A')


class VocabularyTests(SimpleTestCase):
    # form: (change, administrator, delay event, early warning meeting, time extension)
    TABLE = {
        'NEC4_ECC': ('compensation event', 'Project Manager', 'compensation event', 'early warning meeting',
                     'change to the Completion Date'),
        'JCT_IC': ('variation', 'Contract Administrator', 'Relevant Event', 'risk review meeting', 'extension of time'),
        'JCT_SBC': ('variation', 'Contract Administrator', 'Relevant Event', 'risk review meeting', 'extension of time'),
        'JCT_MW': ('variation', 'Contract Administrator', 'Relevant Event', 'risk review meeting', 'extension of time'),
        'JCT_DB': ('variation', "Employer's Agent", 'Relevant Event', 'risk review meeting', 'extension of time'),
        'BESPOKE': ('variation', 'contract administrator', 'delay event', 'risk review meeting', 'extension of time'),
        '': ('variation', 'contract administrator', 'delay event', 'risk review meeting', 'extension of time'),
    }

    def test_table(self):
        for form, expected in self.TABLE.items():
            with self.subTest(form=form):
                v = vocabulary(form)
                self.assertEqual((v['change'], v['administrator'], v['delay_event'], v['early_warning_meeting'],
                                  v['time_extension']), expected)

    def test_nec_never_uses_jct_words_and_back(self):
        nec, jct = vocabulary('NEC4_ECC'), vocabulary('JCT_IC')
        self.assertNotIn('Relevant Event', ' '.join(map(str, nec.values())))
        self.assertNotIn('compensation event', ' '.join(map(str, jct.values())))
        self.assertEqual(nec['instruction'], "Project Manager's instruction")
        self.assertEqual(jct['notice_of_delay'], 'notice of delay')
        self.assertEqual(nec['notice_of_delay'], '')

    def test_callers_get_a_copy(self):
        vocabulary('NEC4_ECC')['change'] = 'oops'
        self.assertEqual(vocabulary('NEC4_ECC')['change'], 'compensation event')


class ContractViewTests(CommercialTestBase):
    def test_who_can_open_the_contract(self):
        url = self.url('contract_detail', self.pa.pk)
        expected = {'alice': 200, 'adam': 200, 'pat': 200, 'eve': 404,  # estimating staff can't open live jobs
                    'carla': 403, 'connie': 403, 'sid': 403, 'bea': 403, 'nora': 403, 'bob': 404}
        for name, code in expected.items():
            with self.subTest(name):
                self.assertEqual(self.status_for(getattr(self, name), url), code)
        self.assertEqual(self.status_for(None, url), 302)  # to login

    def test_set_up_fills_presets_and_keeps_the_sum_on_the_project(self):
        self.login(self.pat)
        r = self.client.post(self.url('contract_edit', self.pa.pk), {
            'form': 'JCT_IC', 'edition': '2016', 'contract_sum': '125000.50', 'start_date': '2026-11-02',
            'completion_date': '2027-03-26', 'administrator': self.carla_member.pk, 'lads_rate': '2500',
            'lads_period': 'WEEK', 'retention_released_at_pc_percent': '50', 'valuation_day': '28',
            'bank_holiday_region': 'england-and-wales', 'client_response_days': '14'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            c = MainContract.objects.get(project=self.pa)
            p = Project.objects.get(pk=self.pa.pk)
        self.assertEqual((c.due_date_offset_days, c.final_date_offset_days, c.pay_less_notice_days,
                          c.retention_percent, c.defects_period_months), (7, 14, 5, Decimal('5'), 6))
        self.assertEqual(p.value, Decimal('125000.50'))
        self.assertEqual((p.start_date, p.completion_date), (D('2026-11-02'), D('2027-03-26')))
        self.assertEqual(c.contract_sum, Decimal('125000.50'))  # read through to the project, never copied
        stored = {f.name for f in MainContract._meta.concrete_fields}
        self.assertFalse(stored & {'contract_sum', 'value', 'start_date', 'completion_date'})  # one source of truth
        r = self.client.get(self.url('contract_detail', self.pa.pk))
        self.assertContains(r, 'Contract Administrator')
        self.assertContains(r, 'Carla Client')
        self.assertContains(r, 'check your contract')
        self.assertContains(r, 'Advisory')

    def test_nec_set_up_gets_damages_per_day(self):
        self.login(self.pat)
        self.client.post(self.url('contract_edit', self.pa.pk), {
            'form': 'NEC4_ECC', 'nec_main_option': 'A', 'lads_period': 'WEEK', 'bank_holiday_region': 'scotland',
            'client_response_days': '10', 'retention_released_at_pc_percent': '50', 'valuation_day': '25'})
        with org_context(self.org_a):
            c = MainContract.objects.get(project=self.pa)
        self.assertEqual((c.lads_period, c.bank_holiday_region, c.client_response_days), ('DAY', 'scotland', 10))

    def test_nec_needs_a_main_option(self):
        self.login(self.pat)
        r = self.client.post(self.url('contract_edit', self.pa.pk), {'form': 'NEC4_ECC', 'lads_period': 'DAY',
                                                                     'retention_released_at_pc_percent': '50',
                                                                     'bank_holiday_region': 'england-and-wales',
                                                                     'client_response_days': '14'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Pick the main option')

    def test_reset_to_standard_terms(self):
        with org_context(self.org_a):
            MainContract.objects.create(project=self.pa, form='JCT_SBC', pay_less_notice_days=9)
        self.login(self.pat)
        self.client.post(self.url('contract_edit', self.pa.pk), {'action': 'preset'})
        with org_context(self.org_a):
            self.assertEqual(MainContract.objects.get(project=self.pa).pay_less_notice_days, 5)

    def test_external_cannot_post(self):
        for user in (self.carla, self.sid):
            self.assertEqual(self.status_for(user, self.url('contract_edit', self.pa.pk), 'post', {'form': 'JCT_IC'}), 403)
        with org_context(self.org_a):
            self.assertFalse(MainContract.objects.exists())

    def test_vocabulary_for_project(self):
        with org_context(self.org_a):
            self.assertEqual(vocabulary_for(self.pa)['change'], 'variation')
            MainContract.objects.create(project=self.pa, form='NEC4_ECC', nec_main_option='A')
            self.assertEqual(vocabulary_for(Project.objects.get(pk=self.pa.pk))['administrator'], 'Project Manager')

    def test_commercial_tab_replaces_variations_for_staff_only(self):
        self.login(self.pat)
        r = self.client.get(self.url('project_detail', self.pa.pk))
        self.assertContains(r, '>Commercial<')
        self.assertNotContains(r, 'Client variations</a>')
        self.client.logout()
        self.login(self.carla)
        r = self.client.get(self.url('project_detail', self.pa.pk))
        self.assertNotContains(r, 'Commercial')
        # Externals' client variations sit under their "Waiting on you" tab (actions/tabs.py)
        r = self.client.get(self.url('project_decisions', self.pa.pk))
        self.assertNotContains(r, 'Commercial')
        self.assertContains(r, self.url('variation_list', self.pa.pk))


class ContractTenancyTests(CommercialTestBase):
    def test_other_company_gets_404_and_cannot_read(self):
        with org_context(self.org_a):
            MainContract.objects.create(project=self.pa, form='JCT_IC')
        self.login(self.bob)
        self.assertEqual(self.client.get(self.url('contract_detail', self.pa.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.url('contract_detail', self.pa.pk, org=self.org_b)).status_code, 404)
        with org_context(self.org_b):
            self.assertFalse(MainContract.objects.exists())

    def test_no_org_context_fails_closed(self):
        with self.assertRaises(TenantContextMissing):
            MainContract.objects.count()


class ValuationDayTests(CommercialTestBase):
    """JCT IC, ICD, SBC, DB and NEC4 pay from a monthly valuation (assessment) date: the day is required, never guessed."""
    TABLE = {'JCT_MW': False, 'JCT_MWD': False, 'JCT_IC': True, 'JCT_ICD': True, 'JCT_SBC': True, 'JCT_DB': True,
             'NEC4_ECC': True, 'BESPOKE': False}

    def post(self, form, **extra):
        data = {'form': form, 'edition': '2016' if form.startswith('JCT_') else '', 'nec_main_option': 'A',
                'lads_period': 'WEEK', 'retention_released_at_pc_percent': '50',
                'bank_holiday_region': 'england-and-wales', 'client_response_days': '14', **extra}
        return self.client.post(self.url('contract_edit', self.pa.pk), data)

    def test_table(self):
        self.assertEqual(set(self.TABLE), {code for code, _ in MainContract.FORM_CHOICES})
        for form, needed in self.TABLE.items():
            with self.subTest(form=form):
                self.assertIs(presets.uses_valuation_dates(form), needed)
        self.assertNotIn('valuation_day', presets.preset_for('JCT_IC').values())  # never defaulted

    def test_required_on_set_up_and_on_save(self):
        self.login(self.pat)
        for form, needed in self.TABLE.items():
            with self.subTest(form=form):
                r = self.post(form)
                if needed:
                    self.assertEqual(r.status_code, 200)
                    self.assertContains(r, 'Enter the valuation day')
                    with org_context(self.org_a):
                        self.assertFalse(MainContract.objects.exists())
                else:
                    self.assertEqual(r.status_code, 302)
                    with org_context(self.org_a):
                        MainContract.objects.all().delete()
        r = self.post('JCT_IC', valuation_day='25')
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.assertEqual(MainContract.objects.get(project=self.pa).valuation_day, 25)
        # Saving it again without the day is refused too
        r = self.post('JCT_IC', valuation_day='')
        self.assertContains(r, 'Enter the valuation day')
        with org_context(self.org_a):
            self.assertEqual(MainContract.objects.get(project=self.pa).valuation_day, 25)

    def test_help_text_on_the_form(self):
        self.login(self.pat)
        r = self.client.get(self.url('contract_edit', self.pa.pk))
        self.assertContains(r, 'Interim Valuation Dates in the JCT Contract Particulars')

    def test_out_of_range_day_is_refused_once(self):
        self.login(self.pat)
        r = self.post('JCT_IC', valuation_day='32')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'Enter the valuation day')  # the range error, not a second message

    def test_contract_page_asks_for_a_missing_day(self):
        with org_context(self.org_a):
            c = MainContract.objects.create(project=self.pa, form='JCT_IC')
            self.assertTrue(c.needs_valuation_day)
            self.assertFalse(MainContract(form='JCT_MW').needs_valuation_day)
        self.login(self.pat)
        r = self.client.get(self.url('contract_detail', self.pa.pk))
        self.assertContains(r, 'Set the valuation day')
        self.assertContains(r, self.url('contract_edit', self.pa.pk))
