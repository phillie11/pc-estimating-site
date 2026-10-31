"""
The money and date rules for applications for payment. ADVISORY: check your contract.

Pure functions of numbers and dates: no database and no "now" (callers pass `today`). Every rule
here is held to a fixed table in payments/tests/test_rules.py, which is what the product owner
signs off. Contract terms (days, retention, region) come from contracts.MainContract and its
presets (contracts/presets.py); days are counted by holidays.calendar (HGCRA s116: from the day
after, leaving out Christmas Day, Good Friday and the region's bank holidays).

The payment cycle (HGCRA ss109-111, as amended by the 2009 Act; the Scheme Part II; JCT section 4;
NEC4 clause 50-51 with Option Y(UK)2):

  valuation (assessment) date
    + due_date_offset_days    -> due date
                                 (the Scheme only: or the date the claim is made, if later, para 4)
    + payment_notice_days     -> payer's payment notice deadline, counted from the due date
                                 (s110A: no later than 5 days after it; JCT Interim Certificate,
                                 NEC4 payment certificate)
    + final_date_offset_days  -> final date for payment, counted from the due date
    - pay_less_notice_days    -> pay less notice deadline, counted back from the final date (s111)

Notified sum (s111): what must be paid by the final date.
  1. a pay less notice given by its deadline: the sum in it;
  2. otherwise a payment notice given by its deadline: the sum in it;
  3. otherwise the application itself (s110B: the payee's application stands as the default
     payment notice when the payer gives none in time).
A pay less notice after its deadline has no effect, so the screens refuse it.

Valuation:
  gross   = work done to date + approved client variations to date + materials on and off site
  retention held = gross x retention % x the share still held, rounded half up to 1p:
            all of it before practical completion (Completion under NEC), the share not released
            at completion (usually half) until the end of the defects (rectification) period,
            none after that
  net     = gross - retention held
  due     = net - previous amounts (the notified sums of earlier applications)
  VAT     = due x VAT rate, rounded half up to 1p; nil where the domestic reverse charge applies
            (VAT Act 1994 s55A), with a note that the client accounts for it.
CIS is not calculated here.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from changes.clocks import TONE_DONE, TONE_LATE, TONE_OK, TONE_SOON, Clock
from contracts import presets
from holidays.calendar import DEFAULT_REGION, add_days

ADVISORY = 'Advisory: check your contract.'
PENNY = Decimal('0.01')
ZERO = Decimal('0.00')
VAT_STANDARD_RATE = Decimal('20')
DUE_SOON_DAYS = 7  # a deadline this close shows as "soon"
REVERSE_CHARGE_NOTE = ('Domestic reverse charge applies: customer to account to HMRC for the VAT on this '
                       'application (VAT Act 1994 s55A). No VAT is charged. Advisory: check with your accountant.')

STAGE_BEFORE_PC, STAGE_AFTER_PC, STAGE_AFTER_DEFECTS = 'BEFORE_PC', 'AFTER_PC', 'AFTER_DEFECTS'
STAGE_CHOICES = [
    (STAGE_BEFORE_PC, 'Before completion: full retention held'),
    (STAGE_AFTER_PC, 'After completion: completion share released'),
    (STAGE_AFTER_DEFECTS, 'After the defects period: all retention released'),
]

BASIS_PAY_LESS, BASIS_PAYMENT_NOTICE, BASIS_DEFAULT = 'pay_less', 'payment_notice', 'default'
BASIS_LABELS = {
    BASIS_PAY_LESS: 'the pay less notice',
    BASIS_PAYMENT_NOTICE: 'the payment notice',
    BASIS_DEFAULT: 'the application (no payment notice in time: it stands as the default notice, s110B)',
}


def money(value):
    """A Decimal rounded half up to 1p. Never pass a float."""
    if isinstance(value, float):
        raise TypeError('Money must be Decimal, not float.')
    return Decimal(value or 0).quantize(PENNY, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def add_months(day, months):
    """The same day `months` later, or the month's last day when it is shorter (31 Jan + 1 = 28/29 Feb)."""
    month0 = day.month - 1 + months
    year, month = day.year + month0 // 12, month0 % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def defects_end(completion_on, defects_months):
    """End of the defects (rectification) period, or None if either is unknown."""
    if completion_on is None or defects_months is None:
        return None
    return add_months(completion_on, defects_months)


def retention_stage(valuation_date, completion_on, defects_months):
    """
    Which retention applies at this valuation date: before completion; from the completion date
    (practical completion certified on or before the valuation date); or from the end of the
    defects period. With no defects period set, it never moves past completion on its own.
    """
    if completion_on is None or valuation_date < completion_on:
        return STAGE_BEFORE_PC
    end = defects_end(completion_on, defects_months)
    if end is not None and valuation_date >= end:
        return STAGE_AFTER_DEFECTS
    return STAGE_AFTER_PC


def retention_held(gross, percent, released_at_pc_percent, stage):
    """Retention held on a gross valuation at this stage, rounded half up to 1p."""
    if not percent or stage == STAGE_AFTER_DEFECTS:
        return ZERO
    share = Decimal('100') if stage == STAGE_BEFORE_PC else Decimal('100') - Decimal(released_at_pc_percent)
    return money(Decimal(gross) * Decimal(percent) / 100 * share / 100)


# ---------------------------------------------------------------------------
# The valuation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Valuation:
    work_done: Decimal
    variations: Decimal
    materials_on_site: Decimal
    materials_off_site: Decimal
    gross: Decimal
    retention: Decimal
    net: Decimal
    previous: Decimal
    amount_due: Decimal
    vat_rate: Decimal
    vat: Decimal
    total: Decimal
    reverse_charge: bool
    stage: str


def vat_on(amount, rate=VAT_STANDARD_RATE, reverse_charge=False):
    """VAT on an amount, half up to 1p; nil under the domestic reverse charge."""
    if reverse_charge:
        return ZERO
    return money(Decimal(amount) * Decimal(rate) / 100)


def valuation(work_done, variations=ZERO, materials_on_site=ZERO, materials_off_site=ZERO, retention_percent=None,
              released_at_pc_percent=Decimal('50'), stage=STAGE_BEFORE_PC, previous=ZERO, vat_rate=VAT_STANDARD_RATE,
              reverse_charge=False):
    work_done, variations = money(work_done), money(variations)
    mos, mos_off = money(materials_on_site), money(materials_off_site)
    gross = work_done + variations + mos + mos_off
    retention = retention_held(gross, retention_percent, released_at_pc_percent, stage)
    net = gross - retention
    previous = money(previous)
    due = net - previous
    vat = vat_on(due, vat_rate, reverse_charge)
    return Valuation(work_done, variations, mos, mos_off, gross, retention, net, previous, due, Decimal(vat_rate),
                     vat, due + vat, bool(reverse_charge), stage)


def ex_vat(amount_inc_vat, rate=VAT_STANDARD_RATE, reverse_charge=False):
    """The part of a VAT-inclusive amount (a payment received) that is not VAT, half up to 1p."""
    if reverse_charge:
        return money(amount_inc_vat)
    return money(Decimal(amount_inc_vat) * 100 / (100 + Decimal(rate)))


def lines_total(values):
    """Work done to date from lines by section: their sum, each rounded to 1p first."""
    return sum((money(v) for v in values), ZERO)


# ---------------------------------------------------------------------------
# The payment cycle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PaymentCycle:
    valuation_date: date
    due_date: date
    payment_notice_by: date
    pay_less_by: date
    final_date: date


@dataclass(frozen=True)
class Terms:
    due_date_offset_days: int
    payment_notice_days: int
    final_date_offset_days: int
    pay_less_notice_days: int
    region: str = DEFAULT_REGION
    scheme: bool = False  # the Scheme's "or when the claim is made, if later" applies to the due date
    label: str = ''


def terms_for(contract):
    """
    The payment terms for a project's contract: its own figures, any left blank taken from its
    form's standard terms; the Scheme when there is no contract. Only the Scheme preset itself
    (Preset.is_scheme) brings the Scheme's "due on the claim date, if later" rule: a bespoke contract
    that starts from the Scheme's periods does not.
    """
    if contract is None:
        p = presets.SCHEME
        return Terms(p.due_date_offset_days, p.payment_notice_days, p.final_date_offset_days, p.pay_less_notice_days,
                     DEFAULT_REGION, scheme=p.is_scheme, label=p.label)
    p = contract.preset

    def pick(name):
        value = getattr(contract, name)
        return getattr(p, name) if value is None else value
    return Terms(pick('due_date_offset_days'), pick('payment_notice_days'), pick('final_date_offset_days'),
                 pick('pay_less_notice_days'), contract.bank_holiday_region or DEFAULT_REGION,
                 scheme=p.is_scheme, label=p.label)


def payment_cycle(valuation_date, terms, claim_date=None):
    """
    Every date in the cycle for a valuation date. The due date, final date and pay less deadline
    come from contracts.presets.payment_timetable; the payment notice deadline is counted the same
    way from the due date. Under the Scheme an application made after the period's due date moves
    the due date to the day it is made (para 4(b)).
    """
    t = presets.payment_timetable(valuation_date, terms.due_date_offset_days, terms.final_date_offset_days,
                                  terms.pay_less_notice_days, region=terms.region)
    due = t.due_date
    if terms.scheme and claim_date is not None and claim_date > due:
        t = presets.payment_timetable(claim_date, 0, terms.final_date_offset_days, terms.pay_less_notice_days,
                                      region=terms.region)
        due = t.due_date
    return PaymentCycle(valuation_date, due, add_days(due, terms.payment_notice_days, terms.region),
                        t.pay_less_by, t.final_date)


# ---------------------------------------------------------------------------
# Notices and the notified sum
# ---------------------------------------------------------------------------

def in_time(given_on, deadline):
    """A notice counts if given on or before its deadline day."""
    return given_on is not None and deadline is not None and given_on <= deadline


def notified_sum(applied, payment_notice_amount=None, payment_notice_on=None, payment_notice_by=None,
                 pay_less_amount=None, pay_less_on=None, pay_less_by=None):
    """(sum, basis): what must be paid by the final date (excluding VAT), and why (BASIS_*)."""
    if pay_less_amount is not None and in_time(pay_less_on, pay_less_by):
        return money(pay_less_amount), BASIS_PAY_LESS
    if payment_notice_amount is not None and in_time(payment_notice_on, payment_notice_by):
        return money(payment_notice_amount), BASIS_PAYMENT_NOTICE
    return money(applied), BASIS_DEFAULT


def stands_as_notified(today, payment_notice_by, payment_notice_on):
    """True once the payment notice deadline has passed with no payment notice in time (s110B)."""
    return payment_notice_by is not None and today > payment_notice_by and not in_time(payment_notice_on,
                                                                                         payment_notice_by)


def pay_less_refusal(given_on, pay_less_by):
    """Why a pay less notice given on this day can't be accepted, or '' if it can."""
    if pay_less_by is None:
        return 'This application has no pay less notice deadline yet.'
    if given_on > pay_less_by:
        late = (given_on - pay_less_by).days
        return (f'Too late: the last day for a pay less notice was {pay_less_by:%a} {pay_less_by.day} '
                f'{pay_less_by:%b %Y}, {late} day{"s" if late != 1 else ""} ago. A pay less notice after the '
                'deadline has no effect, so the notified sum must be paid in full by the final date (HGCRA s111). '
                + ADVISORY)
    return ''


def outstanding(notified_total, paid):
    return money(notified_total) - money(paid)


def is_payment_overdue(today, final_date, outstanding_amount):
    return final_date is not None and today > final_date and outstanding_amount > 0


# ---------------------------------------------------------------------------
# Clocks, as changes/clocks.py shows them
# ---------------------------------------------------------------------------

def _d(day):
    return f'{day:%a} {day.day} {day:%b %Y}'


def _left(deadline, today, todo, late):
    left = (deadline - today).days
    if left < 0:
        return left, TONE_LATE, f'{-left} day{"s" if left != -1 else ""} past. {late}'
    if left == 0:
        return left, TONE_SOON, f'Today. {todo}'
    if left <= DUE_SOON_DAYS:
        return left, TONE_SOON, f'{left} day{"s" if left != 1 else ""} left. {todo}'
    return left, TONE_OK, f'{left} days left. {todo}'


def payment_clocks(cycle, today, submitted_on=None, payment_notice_on=None, pay_less_on=None, paid_in_full_on=None,
                   words=None):
    """
    The cycle as clocks (changes.clocks.Clock), in date order: valuation, due date, payment notice,
    pay less notice, final date for payment. Advisory.
    """
    if cycle is None:
        return []
    words = words or {}
    notice = words.get('payment_document', 'payment notice')
    out = []
    if submitted_on is not None:
        late = (submitted_on - cycle.valuation_date).days
        out.append(Clock('valuation', words.get('valuation_date', 'valuation date').capitalize(), cycle.valuation_date,
                         None, TONE_DONE, f'Application submitted {_d(submitted_on)}'
                         + (f', {late} day{"s" if late != 1 else ""} after the valuation date.' if late > 0 else '.'),
                         'Contract', submitted_on))
    else:
        left, tone, msg = _left(cycle.valuation_date, today, 'Submit the application by then.',
                                'Submit the application as soon as you can.')
        out.append(Clock('valuation', words.get('valuation_date', 'valuation date').capitalize(), cycle.valuation_date,
                         left, tone, msg, 'Contract'))
    due_left = (cycle.due_date - today).days
    out.append(Clock('due', 'Due date', cycle.due_date, None if due_left < 0 else due_left,
                     TONE_DONE if due_left < 0 else TONE_OK, 'Payment fell due.' if due_left < 0 else
                     'Payment falls due. The final date for payment follows.', 'HGCRA s109'))
    if payment_notice_on is not None:
        if payment_notice_on <= cycle.payment_notice_by:
            out.append(Clock('payment_notice', 'Payment notice', cycle.payment_notice_by, None, TONE_DONE,
                             f'Given {_d(payment_notice_on)}, in time.', 'HGCRA s110A', payment_notice_on))
        else:
            out.append(Clock('payment_notice', 'Payment notice', cycle.payment_notice_by, None, TONE_LATE,
                             f'Given {_d(payment_notice_on)}, after the deadline, so the application stands as the '
                             'notified sum (HGCRA s110B). The payer can still give a pay less notice in time. '
                             + ADVISORY, 'HGCRA s110B', payment_notice_on))
    elif submitted_on is not None and today > cycle.payment_notice_by:
        out.append(Clock('payment_notice', 'Payment notice', cycle.payment_notice_by, -(today - cycle.payment_notice_by).days,
                         TONE_LATE, 'No payment notice by the deadline: the application stands as the notified sum '
                         '(default payment notice, HGCRA s110B). ' + ADVISORY, 'HGCRA s110B'))
    else:
        left, tone, msg = _left(cycle.payment_notice_by, today, f'The payer gives their payment notice ({notice}).',
                                'Missed.')
        out.append(Clock('payment_notice', 'Payment notice', cycle.payment_notice_by, left, tone, msg, 'HGCRA s110A'))
    if pay_less_on is not None:
        out.append(Clock('pay_less', 'Pay less notice', cycle.pay_less_by, None, TONE_DONE,
                         f'Given {_d(pay_less_on)}, in time.', 'HGCRA s111', pay_less_on))
    elif today > cycle.pay_less_by:
        out.append(Clock('pay_less', 'Pay less notice', cycle.pay_less_by, None, TONE_DONE,
                         'Deadline passed with no pay less notice: the notified sum is payable in full.', 'HGCRA s111'))
    else:
        left, tone, msg = _left(cycle.pay_less_by, today, 'Last day for a pay less notice, if the payer intends to '
                                'pay less than the notified sum.', '')
        out.append(Clock('pay_less', 'Pay less notice', cycle.pay_less_by, left, tone, msg, 'HGCRA s111'))
    if paid_in_full_on is not None:
        late = (paid_in_full_on - cycle.final_date).days
        out.append(Clock('final', 'Final date for payment', cycle.final_date, None, TONE_LATE if late > 0 else TONE_DONE,
                         f'Paid in full {_d(paid_in_full_on)}' + (f', {late} day{"s" if late != 1 else ""} late.'
                                                                  if late > 0 else ', in time.'),
                         'HGCRA s110', paid_in_full_on))
    else:
        left, tone, msg = _left(cycle.final_date, today, 'The notified sum must be paid by then.',
                                'Payment is overdue. You may be entitled to interest and to suspend work after '
                                'giving 7 days\' notice (HGCRA s112). ' + ADVISORY)
        out.append(Clock('final', 'Final date for payment', cycle.final_date, left, tone, msg, 'HGCRA s110'))
    return out


def next_deadline_after(day, cycle):
    """The first cycle date on or after `day` (for "what's next"), or None."""
    dates = [cycle.payment_notice_by, cycle.pay_less_by, cycle.final_date]
    return min((d for d in dates if d >= day), default=None)


def submission_window(valuation_day, today, warn_days=7):
    """
    The valuation dates an application could be due for: the latest one on or before today and
    the next one, if it is within warn_days. Monthly, day N of the month (above 28 snaps to the
    month's end), as contracts.presets.next_valuation_date.
    """
    if not valuation_day:
        return []
    nxt = presets.next_valuation_date(today, valuation_day)
    prev_month = today.replace(day=1) - timedelta(days=1)
    this = presets.valuation_date_in(today.year, today.month, valuation_day)
    latest = this if this <= today else presets.valuation_date_in(prev_month.year, prev_month.month, valuation_day)
    out = [latest]
    if nxt != latest and (nxt - today).days <= warn_days:
        out.append(nxt)
    return out
