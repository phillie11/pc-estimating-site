"""
A job with money across three months, made through the owning modules' own services, so the CVR
is tested against what payments, costs and contracts really produce.

  two months ago (M2)  AP-001 valued on the 25th: work done 20,000 (gross 20,000, 5% retention)
                       invoice 4,000 and timesheet 3,000 on the ledger
  last month (M1)      AP-001 paid 22,800 including VAT (19,000 ex VAT) on the 10th
                       VO approved on the 5th at 6,000
                       AP-002 valued on the 25th: work done 50,000 + the VO (gross 56,000)
                       invoice 9,500 on the 20th
  this month (M0)      budget set (dated today), joinery order committed, a forecast set by hand,
                       timesheet 1,250 today

As in changes/tests/base.py: alice owner, adam admin, pat the job's PM, eve estimator, carla the
client, connie consultant, sid subcontractor, bea tendering sub, nora external, bob another company.
"""
from datetime import date, datetime, time
from decimal import Decimal as D

from django.utils import timezone

from changes.tests.base import CommercialTestBase, make_user
from contracts.models import MainContract
from core.workflow import apply_transition
from costs import services as cost_services
from costs.access import settings_for as cost_settings_for
from costs.models import CostCode
from cvr import rules
from directory.models import Package
from payments.models import Application, Payment
from tenancy.context import org_context
from tenancy.models import Membership
from variations.models import Variation


def aware(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour, 0)))


class CVRTestBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.m0 = rules.month_start(self.today)
        self.m1 = rules.add_months(self.m0, -1)
        self.m2 = rules.add_months(self.m0, -2)
        self.stan = make_user('stan@a.test', 'Stan Site')  # staff on live jobs, not this job's PM or QS
        Membership.objects.create(organization=self.org_a, user=self.stan, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)

    def day(self, month, n):
        return month.replace(day=n)

    def build_money(self):
        """The job's money, as the docstring says. Returns a dict of the rows made."""
        out = {}
        with org_context(self.org_a):
            self.pa.start_date = self.day(self.m2, 1)
            self.pa.completion_date = rules.add_months(self.m0, 3).replace(day=20)
            self.pa.save()
            contract = MainContract(project=self.pa, form='JCT_IC', retention_percent=D('5'), valuation_day=25,
                                    defects_period_months=12)
            contract.apply_preset(only_blank=True, creating=True)
            contract.save()
            cost_settings_for(self.org_a)
            codes = {c.code: c for c in CostCode.objects.all()}
            self.codes = codes

            ap1 = Application(project=self.pa, valuation_date=self.day(self.m2, 25), work_done=D('20000'),
                              title='Valuation 1')
            ap1.recalculate(contract)
            ap1.save(actor=self.pat)
            apply_transition(ap1, 'submit', self.pat)
            Payment.objects.create(application=ap1, amount=D('22800.00'), received_on=self.day(self.m1, 10),
                                   recorded_by=self.pat)
            ap1._paid_total = None
            apply_transition(ap1, 'paid', self.pat, paid_on=self.day(self.m1, 10))

            vo = Variation(project=self.pa, title='Marble bar top', description='Supply and fit.', value=D('6500'))
            vo.save(actor=self.pat)
            Variation.objects.filter(pk=vo.pk).update(status=Variation.STATUS_APPROVED, approved_value=D('6000'),
                                                      decided_at=aware(self.day(self.m1, 5)))

            ap2 = Application(project=self.pa, valuation_date=self.day(self.m1, 25), work_done=D('50000'),
                              title='Valuation 2')
            ap2.recalculate(contract)
            ap2.save(actor=self.pat)
            apply_transition(ap2, 'submit', self.pat)

            cost_services.record_actual(self.pa, self.pat, codes['400'], 'INVOICE', 'Timber Co', D('4000'),
                                        cost_date=self.day(self.m2, 12))
            cost_services.record_actual(self.pa, self.pat, codes['300'], 'TIMESHEET', 'Site joiners', D('3000'),
                                        cost_date=self.day(self.m2, 20))
            cost_services.record_actual(self.pa, self.pat, codes['400'], 'INVOICE', 'Timber Co', D('9500'),
                                        cost_date=self.day(self.m1, 20))
            cost_services.set_original_budget(self.pa, self.pat, D('100000'), D('10'), [
                (codes['220'], '', D('50000')), (codes['300'], '', D('20000')), (codes['400'], '', D('20000'))])
            pkg = Package.objects.create(project=self.pa, subcontractor=self.subco, status=Package.APPOINTED,
                                         quote_value=D('48000'))
            cost_services.commit_package(pkg, codes['220'], self.pat)
            cost_services.set_forecast(self.pa, self.pat, codes['300'], D('24000'), 'More labour than planned')
            cost_services.record_actual(self.pa, self.pat, codes['300'], 'TIMESHEET', 'Site joiners', D('1250'),
                                        cost_date=self.today)
            out.update(contract=contract, ap1=ap1, ap2=ap2, vo=vo, pkg=pkg)
        return out
