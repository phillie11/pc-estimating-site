"""
Advisory contract clocks for change events. ADVISORY: check your contract. Amendments (Z
clauses, a changed "period for reply", a deleted time bar) override every date here, and
these functions only know the dates they are given.

Pure functions of dates: no database, no "now". Callers pass `today`.

NEC4 ECC (core clauses as published):
  61.3  The Contractor notifies a compensation event within 8 weeks of becoming aware of it,
        or loses any change to the Prices, Completion Date or Key Dates (the time bar), unless
        the Project Manager should have notified it.
  62.3  A quotation is due 3 weeks after the Project Manager instructs it. The Project Manager
        replies within 2 weeks of receiving it.
  62.6  If the Project Manager doesn't reply in time, the Contractor may notify that failure.
        If it continues for a further 2 weeks after that notice, the quotation is treated as
        accepted (deemed acceptance).
Siteline counts these NEC periods as plain calendar weeks of 7 days, with weekends and bank
holidays counted like any other day. That is a simplification, not a rule taken from a clause:
check the Contract Data and any Z clauses for how your contract counts time. (Payment dates are
different: the Construction Act leaves bank holidays out, see holidays/calendar.py.)

Deemed acceptance: the quotation is treated as accepted once the failure has continued for a
further 2 weeks after the Contractor's notice, so it takes effect the day after notice + 14 days.
A reply on or before notice + 14 days still counts.

JCT has no time-barred notification of variations. What it does require, in every form, is
written notice "forthwith" once it becomes reasonably apparent that progress is or is likely to
be delayed (MW 2.7, IC 2.19, SBC 2.27, DB 2.24 in the 2016 editions), so a time-impacting change
event gets a prompt instead of a countdown.
"""
from dataclasses import dataclass
from datetime import date, timedelta

ADVISORY = 'Advisory: check your contract.'

NOTIFICATION_WEEKS = 8   # NEC4 61.3
QUOTATION_WEEKS = 3      # NEC4 62.3
REPLY_WEEKS = 2          # NEC4 62.3
DEEMED_WEEKS = 2         # NEC4 62.6
DUE_SOON_DAYS = 7        # warn this many days ahead
OPEN_FOR_CLOCKS = ('IDENTIFIED', 'NOTIFIED', 'QUOTED')  # ChangeEvent statuses whose clocks still run

TONE_DONE, TONE_OK, TONE_SOON, TONE_LATE = 'good', 'info', 'warn', 'bad'


def _d(day):
    return f'{day.day} {day:%b %Y}'


def weeks_after(day, weeks):
    """Plain calendar weeks: no working-day or bank holiday adjustment (advisory, see the module docstring)."""
    return day + timedelta(weeks=weeks)


@dataclass(frozen=True)
class Clock:
    key: str
    label: str
    deadline: date
    days_left: int | None  # None once done; negative when late
    tone: str
    message: str
    clause: str
    done_on: date | None = None

    @property
    def days_late(self):
        return -self.days_left if self.days_left is not None and self.days_left < 0 else 0

    @property
    def is_late(self):
        return self.tone == TONE_LATE

    @property
    def needs_attention(self):
        return self.tone in (TONE_LATE, TONE_SOON)


def _clock(key, label, deadline, done_on, today, clause, todo, done_msg, late_msg):
    if done_on is not None:
        late_by = (done_on - deadline).days
        if late_by > 0:
            return Clock(key, label, deadline, None, TONE_LATE,
                         f'{done_msg} {late_by} day{"s" if late_by != 1 else ""} after the deadline. {late_msg}',
                         clause, done_on)
        return Clock(key, label, deadline, None, TONE_DONE, f'{done_msg} in time.', clause, done_on)
    left = (deadline - today).days
    if left < 0:
        tone, msg = TONE_LATE, f'{-left} day{"s" if left != -1 else ""} overdue. {late_msg}'
    elif left == 0:
        tone, msg = TONE_SOON, f'Due today. {todo}'
    elif left <= DUE_SOON_DAYS:
        tone, msg = TONE_SOON, f'{left} day{"s" if left != 1 else ""} left. {todo}'
    else:
        tone, msg = TONE_OK, f'{left} days left. {todo}'
    return Clock(key, label, deadline, left, tone, msg, clause)


def notification_deadline(aware_on):
    return weeks_after(aware_on, NOTIFICATION_WEEKS)


def quotation_deadline(instructed_on):
    return weeks_after(instructed_on, QUOTATION_WEEKS)


def reply_deadline(submitted_on):
    return weeks_after(submitted_on, REPLY_WEEKS)


def deemed_acceptance_date(submitted_on, failure_notified_on, reply_received_on=None):
    """
    The first day a quotation is treated as accepted (NEC4 62.6), or None. It needs no reply in
    time and a notice of that failure given after the reply period ended. The failure must then
    continue for a further 2 weeks, so acceptance takes effect the day after notice + 14 days. A
    reply on or before notice + 14 days prevents it. A notice given before the reply was late doesn't count.
    """
    if submitted_on is None or failure_notified_on is None:
        return None
    if failure_notified_on <= reply_deadline(submitted_on):
        return None
    last_day_to_reply = weeks_after(failure_notified_on, DEEMED_WEEKS)
    if reply_received_on is not None and reply_received_on <= last_day_to_reply:
        return None
    return last_day_to_reply + timedelta(days=1)


def nec_clocks(today, aware_on=None, notified_on=None, quotation_instructed_on=None, quotation_submitted_on=None,
               reply_received_on=None, failure_notified_on=None, words=None):
    """The NEC4 clocks that apply to a change event with these dates, in order."""
    pm = (words or {}).get('administrator', 'Project Manager')
    clocks = []
    if aware_on is not None:
        clocks.append(_clock(
            'notify', 'Notify the compensation event', notification_deadline(aware_on), notified_on, today, 'NEC4 61.3',
            todo=f'Notify the {pm} within 8 weeks of becoming aware.', done_msg='Notified',
            late_msg='Past the 8-week time bar: you may have lost any change to the Prices or Completion Date, '
                     f'unless the {pm} should have notified it.'))
    if quotation_instructed_on is not None:
        clocks.append(_clock(
            'quote', 'Submit the quotation', quotation_deadline(quotation_instructed_on), quotation_submitted_on,
            today, 'NEC4 62.3', todo='Quotation due 3 weeks after the instruction.', done_msg='Quotation submitted',
            late_msg=f'The {pm} may make their own assessment (NEC4 64.1).'))
    if quotation_submitted_on is not None:
        due = reply_deadline(quotation_submitted_on)
        clocks.append(_clock(
            'reply', f'{pm} replies to the quotation', due, reply_received_on, today, 'NEC4 62.3',
            todo=f'The {pm} should reply within 2 weeks.', done_msg='Reply received',
            late_msg=f'Notify the {pm} that they have not replied; the quotation is treated as accepted if they '
                     'still have not replied 2 weeks after that notice.'))
        if reply_received_on is None or reply_received_on > due:
            deemed = deemed_acceptance_date(quotation_submitted_on, failure_notified_on, reply_received_on)
            if deemed is not None:
                left = (deemed - today).days
                if left <= 0:
                    clocks.append(Clock('deemed', 'Deemed acceptance', deemed, left, TONE_DONE,
                                        f'Treated as accepted on {_d(deemed)}: no reply within 2 weeks of your '
                                        'notice.', 'NEC4 62.6'))
                else:
                    clocks.append(Clock('deemed', 'Deemed acceptance', deemed, left, TONE_SOON if left <= DUE_SOON_DAYS
                                        else TONE_OK, f'Treated as accepted on {_d(deemed)} unless the {pm} '
                                        f'replies by {_d(deemed - timedelta(days=1))} ({left} day{"s" if left != 1 else ""} '
                                        'left).', 'NEC4 62.6'))
            elif failure_notified_on is not None and failure_notified_on <= due:
                clocks.append(Clock('deemed', 'Deemed acceptance', due, None, TONE_SOON,
                                    f'Your notice of failure was given before the reply was late, so it does not '
                                    f'start deemed acceptance. Notify again after {_d(due)}.', 'NEC4 62.6'))
    return clocks


def jct_delay_prompt(time_impact_days, delay_notice_given_on, words=None):
    """A prompt to give notice of delay for a time-impacting change event on a JCT job, or None."""
    if not time_impact_days or time_impact_days <= 0:
        return None
    ca = (words or {}).get('administrator', 'Contract Administrator')
    if delay_notice_given_on is not None:
        return Clock('delay_notice', 'Notice of delay', delay_notice_given_on, None, TONE_DONE,
                     f'Notice of delay given on {_d(delay_notice_given_on)}.', 'JCT', delay_notice_given_on)
    return Clock('delay_notice', 'Notice of delay', None, None, TONE_SOON,
                 f'This could delay completion. Give the {ca} written notice of the delay, its cause and the '
                 'Relevant Event straight away ("forthwith"), then follow up with its expected effects.', 'JCT')


def clocks_for(ce, contract, today):
    """The advisory clocks and prompts for a change event under the project's contract (none if no contract)."""
    if contract is None:
        return []
    words = contract.vocabulary
    if contract.is_nec:
        clocks = nec_clocks(today, aware_on=ce.aware_on, notified_on=ce.notified_on,
                          quotation_instructed_on=ce.quotation_instructed_on,
                          quotation_submitted_on=ce.quotation_submitted_on, reply_received_on=ce.reply_received_on,
                          failure_notified_on=ce.failure_notified_on, words=words)
    elif contract.is_jct:
        prompt = jct_delay_prompt(ce.time_impact_days, ce.delay_notice_given_on, words=words)
        clocks = [prompt] if prompt else []
    else:
        return []
    if ce.status not in OPEN_FOR_CLOCKS:
        # Agreed, rejected or implemented: keep the record of what was done, drop the countdowns
        clocks = [c for c in clocks if c.done_on is not None]
    return clocks
