"""
Money, tax and date rules for paying subcontractors (we are the payer). ADVISORY: check your
contract, and check the subcontractor's status with HMRC before paying.

Pure functions of numbers and dates: no database and no "now" (callers pass `today`). Every rule
is held to a fixed table in subpayments/tests/test_rules.py, which the product owner signs off.

Reused, not copied, from payments.rules (the client side of the same Construction Act cycle):
money(), Terms, payment_cycle() (which uses contracts.presets.payment_timetable and holidays'
s116 day counting, including the statutory Saturday bank holidays), notified_sum(),
stands_as_notified(), pay_less_refusal(), retention_stage(), retention_held(), vat_on() and the
clocks. What is new here is only what the subcontract side needs:

  the cycle         monthly on day N, or a list of dates written into the subcontract
  the valuation     gross value to date + approved sub variations to the valuation date
                    - retention held - previous amounts = sum applied for this time
  discount          the subcontract's discount %, on the sum due this time, half up to 1p
  CIS               deduction = (payment excluding VAT - materials in it) x rate, rounded DOWN
                    to the penny; rates: gross 0%, registered 20%, unregistered/unverified 30%
  VAT               the sub's rate (20% unless stated) on the payment, half up to 1p; nil under
                    the domestic reverse charge (VAT Act 1994 s55A), where we account for it
  tax months        6th of one month to the 5th of the next (CIS300 return periods)
  retention ledger  held, released at practical completion, released at the end of defects
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from contracts import presets
from payments.rules import (  # noqa: F401
    ADVISORY,
    PENNY,
    STAGE_AFTER_DEFECTS,
    STAGE_AFTER_PC,
    STAGE_BEFORE_PC,
    STAGE_CHOICES,
    VAT_STANDARD_RATE,
    ZERO,
    Terms,
    in_time,
    money,
    notified_sum,
    pay_less_refusal,
    payment_clocks,
    payment_cycle,
    retention_held,
    retention_stage,
    stands_as_notified,
    vat_on,
)

HMRC_CHECK = "Check the subcontractor's status with HMRC before paying."
NOT_A_RETURN = ('Figures for your CIS300 monthly return. This is not a submission: nothing is sent to HMRC. '
                'File the return yourself through HMRC Online Services or your payroll software.')
REVERSE_CHARGE_NOTE = ('Domestic reverse charge: VAT Act 1994 s55A applies. No VAT is added: the customer '
                       '(we, the contractor) accounts to HMRC for the VAT. Advisory: check with your accountant.')

# --- CIS --------------------------------------------------------------------------------
CIS_GROSS, CIS_REGISTERED, CIS_UNVERIFIED = 'GROSS', 'REGISTERED', 'UNVERIFIED'
CIS_CHOICES = [
    (CIS_GROSS, 'Gross payment status (0%)'),
    (CIS_REGISTERED, 'Registered (20%)'),
    (CIS_UNVERIFIED, 'Unregistered or not verified (30%)'),
]
CIS_RATES = {CIS_GROSS: Decimal('0'), CIS_REGISTERED: Decimal('20'), CIS_UNVERIFIED: Decimal('30')}
CIS_SHORT = {CIS_GROSS: 'Gross', CIS_REGISTERED: 'Registered', CIS_UNVERIFIED: 'Unverified'}


def cis_rate(status):
    """The deduction rate for a CIS status; anything unknown is treated as unverified (30%)."""
    return CIS_RATES.get(status, CIS_RATES[CIS_UNVERIFIED])


def penny_down(value):
    """Round down (towards zero) to the penny, as HMRC rounds a CIS deduction."""
    return Decimal(value).quantize(PENNY, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class Advice:
    """One payment, as the payment advice shows it."""
    gross: Decimal          # the payment, excluding VAT, before CIS (what posts to the cost ledger)
    materials: Decimal      # cost of materials in it, as the sub stated (staff may adjust)
    labour: Decimal         # gross less materials: what CIS is deducted from
    cis_rate: Decimal
    cis: Decimal            # the deduction, rounded down to the penny
    vat_rate: Decimal
    vat: Decimal            # nil under the reverse charge
    reverse_charge: bool
    net: Decimal            # what the sub receives: gross + VAT - CIS


def cis_deduction(gross_ex_vat, materials, rate):
    """
    The CIS deduction on a payment: (payment excluding VAT - cost of materials) x rate, rounded
    down to the penny. Materials can't take the labour part below nil.
    """
    gross, materials = money(gross_ex_vat), money(materials)
    labour = max(gross - min(materials, gross), ZERO)
    return penny_down(labour * Decimal(rate) / 100)


def advice(gross_ex_vat, materials=ZERO, cis_status=CIS_UNVERIFIED, vat_rate=VAT_STANDARD_RATE,
           reverse_charge=False):
    """Gross due, materials, labour, CIS deduction, VAT and net payable for one payment."""
    gross = money(gross_ex_vat)
    mats = min(money(materials), gross) if gross > 0 else ZERO
    labour = gross - mats
    rate = cis_rate(cis_status)
    cis = cis_deduction(gross, mats, rate)
    vat = vat_on(gross, vat_rate, reverse_charge)
    return Advice(gross, mats, labour, rate, cis, Decimal(vat_rate), vat, bool(reverse_charge), gross + vat - cis)


# --- The valuation ----------------------------------------------------------------------
@dataclass(frozen=True)
class SubValuation:
    gross_to_date: Decimal   # the sub's gross value of work to date (cumulative)
    variations: Decimal      # approved sub variations up to the valuation date
    gross: Decimal
    retention: Decimal
    net: Decimal
    previous: Decimal        # what earlier applications on this subcontract were notified at
    amount_due: Decimal      # the sum applied for this time (excluding VAT)
    stage: str


def sub_valuation(gross_to_date, variations=ZERO, retention_percent=ZERO, released_at_pc_percent=Decimal('50'),
                  stage=STAGE_BEFORE_PC, previous=ZERO):
    gross_to_date, variations = money(gross_to_date), money(variations)
    gross = gross_to_date + variations
    retention = retention_held(gross, retention_percent, released_at_pc_percent, stage)
    net = gross - retention
    previous = money(previous)
    return SubValuation(gross_to_date, variations, gross, retention, net, previous, net - previous, stage)


def discount_on(amount, percent):
    """The subcontract's discount on a sum due, half up to 1p (nil without a discount or on a credit)."""
    if not percent or money(amount) <= 0:
        return ZERO
    return money(money(amount) * Decimal(percent) / 100)


def payable_after_discount(notified, percent):
    return money(notified) - discount_on(notified, percent)


# --- The cycle --------------------------------------------------------------------------
CYCLE_MONTHLY, CYCLE_DATES = 'MONTHLY', 'DATES'
CYCLE_CHOICES = [(CYCLE_MONTHLY, 'Monthly, on a day of the month'), (CYCLE_DATES, 'On dates listed in the subcontract')]


def sub_terms(due_offset, payment_notice_days, final_offset, pay_less_days, region):
    """The subcontract's payment terms in the shape payments.rules.payment_cycle takes."""
    return Terms(due_offset, payment_notice_days, final_offset, pay_less_days, region, scheme=False,
                 label='Subcontract')


def valuation_dates(kind, today, valuation_day=None, listed=()):
    """
    (latest valuation date on or before today, next one after today), either may be None.
    Monthly: day N of each month (N above 28 is the month's last day in shorter months). Listed:
    the dates written into the subcontract.
    """
    if kind == CYCLE_DATES:
        dates = sorted(set(listed))
        past = [d for d in dates if d <= today]
        future = [d for d in dates if d > today]
        return (past[-1] if past else None), (future[0] if future else None)
    if not valuation_day:
        return None, None
    this = presets.valuation_date_in(today.year, today.month, valuation_day)
    if this <= today:
        latest = this
    else:
        prev = today.replace(day=1) - timedelta(days=1)
        latest = presets.valuation_date_in(prev.year, prev.month, valuation_day)
    nxt = presets.next_valuation_date(today + timedelta(days=1), valuation_day)
    return latest, nxt


def parse_dates(text):
    """Dates typed one per line or comma-separated as YYYY-MM-DD or DD/MM/YYYY. Raises ValueError naming the bad one."""
    out = []
    for raw in (text or '').replace(',', '\n').splitlines():
        s = raw.strip()
        if not s:
            continue
        try:
            if '/' in s:
                d, m, y = (int(x) for x in s.split('/'))
                out.append(date(y, m, d))
            else:
                out.append(date.fromisoformat(s))
        except (ValueError, TypeError):
            raise ValueError(f'"{s}" is not a date. Use 25/11/2026 or 2026-11-25, one a line.')
    return sorted(set(out))


def late_notice_warning(today, payment_notice_by, payment_notice_on):
    """The red warning for staff once our payment notice is late, or '' (HGCRA s110B)."""
    if not stands_as_notified(today, payment_notice_by, payment_notice_on):
        return ''
    return (f'We gave no payment notice by {payment_notice_by:%a} {payment_notice_by.day} {payment_notice_by:%b %Y}, '
            "so the subcontractor's application stands as the notified sum (HGCRA s110B, default payment notice). "
            'It must be paid in full by the final date unless we give a pay less notice in time. ' + ADVISORY)


# --- Tax months -------------------------------------------------------------------------
@dataclass(frozen=True)
class TaxMonth:
    start: date
    end: date
    tax_year: str   # "2026-27"
    number: int     # 1 = 6 April to 5 May

    @property
    def key(self):
        return f'{self.start:%Y-%m}'

    @property
    def label(self):
        return f'Tax month {self.number} {self.tax_year}: {self.start.day} {self.start:%b %Y} to {self.end.day} {self.end:%b %Y}'

    @property
    def return_due(self):
        """The CIS300 return is due by the 19th after the tax month ends."""
        return self.end.replace(day=19)


def tax_month(day):
    """The CIS tax month a payment date falls in: the 6th of a month to the 5th of the next."""
    if day.day >= 6:
        start = date(day.year, day.month, 6)
    else:
        prev = day.replace(day=1) - timedelta(days=1)
        start = date(prev.year, prev.month, 6)
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=5)
    first_year = start.year if start.month >= 4 else start.year - 1
    return TaxMonth(start, nxt, f'{first_year}-{(first_year + 1) % 100:02d}', (start.month - 4) % 12 + 1)


def tax_month_from_key(key):
    """'2026-10' -> the tax month starting 6 Oct 2026; None if it isn't one."""
    try:
        y, m = (int(x) for x in (key or '').split('-'))
        return tax_month(date(y, m, 6))
    except (ValueError, TypeError):
        return None


# --- Retention ledger -------------------------------------------------------------------
@dataclass(frozen=True)
class RetentionRow:
    ref: str
    valuation_date: date
    stage: str
    held: Decimal       # retention held after this application
    movement: Decimal   # change since the last one: positive taken, negative released
    released_at: str    # '' / STAGE_AFTER_PC / STAGE_AFTER_DEFECTS when this one released retention


def retention_ledger(rows):
    """
    rows: [(ref, valuation_date, stage, retention held)] for the counted applications, in date
    order. Returns (ledger rows, held now, released at practical completion, released at the end
    of defects). A drop in retention held at a stage after completion is a release at that stage.
    """
    out, last = [], ZERO
    released = {STAGE_AFTER_PC: ZERO, STAGE_AFTER_DEFECTS: ZERO}
    for ref, day, stage, held in rows:
        held = money(held)
        move = held - last
        tag = ''
        if move < 0 and stage in released:
            released[stage] += -move
            tag = stage
        out.append(RetentionRow(ref, day, stage, held, move, tag))
        last = held
    return out, last, released[STAGE_AFTER_PC], released[STAGE_AFTER_DEFECTS]
