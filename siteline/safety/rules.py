"""
Safety rules as pure functions, each with a fixed test table in safety/tests/test_rules.py.

Everything here is ADVISORY: check your contract and your safety adviser. Siteline shows these
rules to help people remember them; it doesn't decide for anyone whether something must be
reported or notified.

The RIDDOR deadlines table and the CDM notifiable (F10) test below are confirmed by the owner as
built (November 2026): change them only with a new decision, and the test tables with them.

RIDDOR 2013 (Reporting of Injuries, Diseases and Dangerous Occurrences Regulations)
    The responsible person (normally the injured worker's employer; for a member of the public,
    whoever is in control of the site) reports to HSE. On a site that is often a subcontractor,
    so the principal contractor checks it has been done.

      category                                 deadline
      DEATH       a death                      notify without delay; report within 10 days
      SPECIFIED   a specified injury to a      notify without delay; report within 10 days
                  worker
      PUBLIC      a member of the public taken notify without delay; report within 10 days
                  straight to hospital
      DANGEROUS   a dangerous occurrence       notify without delay; report within 10 days
      OVER_7      a worker off work or on      report within 15 days of the accident
                  light duties for more than
                  7 days in a row (not counting
                  the day it happened)
      DISEASE     an occupational disease      report as soon as there is a doctor's written
                                               diagnosis (no fixed number of days)
      OVER_3      a worker off for more than   record it (this incident is the record); not
                  3 days but not more than 7   reported to HSE
      ''          not reportable, or not       nothing to report
                  decided yet

    The days are calendar days from the day of the accident (weekends and bank holidays count).

CDM 2015 (Construction (Design and Management) Regulations)
    Notifiable (regulation 6): the construction work is scheduled to last longer than 30 working
    days AND have more than 20 workers on site at the same time at any point, OR to exceed 500
    person-days. The client notifies HSE (form F10) as soon as practicable before the
    construction phase starts; contractors often do it for them.
    Principal designer and principal contractor (regulation 5): the client appoints both in
    writing where there is, or is likely to be, more than one contractor on the project.
    Construction phase plan (regulation 12): the principal contractor draws it up before the
    construction phase starts. Siteline chases it from CPP_WARNING_DAYS before the start on site.

Site sign-in
    "On site now" is everyone who signed in since midnight today (UK time) and hasn't signed out.
    Anyone still signed in from an earlier day shows as "No sign-out recorded" in the history, not
    on the roll call.
"""
import datetime

ADVISORY = 'Advisory: check your contract and your safety adviser.'

# --- RIDDOR -------------------------------------------------------------------------------

RIDDOR_DEATH, RIDDOR_SPECIFIED, RIDDOR_PUBLIC = 'DEATH', 'SPECIFIED', 'PUBLIC'
RIDDOR_DANGEROUS, RIDDOR_OVER_7, RIDDOR_DISEASE, RIDDOR_OVER_3 = 'DANGEROUS', 'OVER_7', 'DISEASE', 'OVER_3'

RIDDOR_CHOICES = [
    ('', 'Not reportable, or not decided yet'),
    (RIDDOR_DEATH, 'Someone died'),
    (RIDDOR_SPECIFIED, 'Specified injury to a worker (fracture, amputation, serious burn, loss of sight, '
                       'crush injury, knocked unconscious)'),
    (RIDDOR_OVER_7, 'Worker off work or on light duties for more than 7 days'),
    (RIDDOR_PUBLIC, 'Member of the public taken straight to hospital'),
    (RIDDOR_DANGEROUS, 'Dangerous occurrence (scaffold or structure collapse, lifting equipment failure, '
                       'hitting a live service)'),
    (RIDDOR_DISEASE, 'Occupational disease diagnosed by a doctor'),
    (RIDDOR_OVER_3, 'Worker off for more than 3 days (record only, not reported)'),
]

# category -> days after the accident the report must reach HSE (None: no fixed number of days)
RIDDOR_DAYS = {
    RIDDOR_DEATH: 10, RIDDOR_SPECIFIED: 10, RIDDOR_PUBLIC: 10, RIDDOR_DANGEROUS: 10,
    RIDDOR_OVER_7: 15, RIDDOR_DISEASE: None,
}
REPORTABLE = frozenset(RIDDOR_DAYS)
WITHOUT_DELAY = frozenset({RIDDOR_DEATH, RIDDOR_SPECIFIED, RIDDOR_PUBLIC, RIDDOR_DANGEROUS})


def is_reportable(category):
    return category in REPORTABLE


def riddor_deadline(category, occurred_on):
    """The last day the report can reach HSE, or None (not reportable, or no fixed deadline)."""
    days = RIDDOR_DAYS.get(category)
    if days is None or occurred_on is None:
        return None
    return occurred_on + datetime.timedelta(days=days)


def riddor_guidance(category, occurred_on=None):
    """One plain sentence saying what to do and by when."""
    if category == RIDDOR_OVER_3:
        return 'Keep this record. It does not need reporting to HSE unless the person is off for more than 7 days.'
    if not is_reportable(category):
        return ''
    deadline = riddor_deadline(category, occurred_on)
    when = f' by {deadline:%a} {deadline.day} {deadline:%b %Y}' if deadline else ''
    if category in WITHOUT_DELAY:
        return f'Report to HSE without delay, and no later than 10 days after it happened{when}.'
    if category == RIDDOR_OVER_7:
        return f'Report to HSE within 15 days of the accident{when}.'
    return 'Report to HSE as soon as you have the doctor\'s written diagnosis.'


def suggest_riddor(anyone_hurt, is_worker, taken_to_hospital, days_off):
    """
    A category to suggest from the injury details, or '' for nothing to suggest. It never picks
    death, specified injury, dangerous occurrence or disease: those need a person's judgement.
    """
    if not anyone_hurt:
        return ''
    if not is_worker:
        return RIDDOR_PUBLIC if taken_to_hospital else ''
    if days_off is not None and days_off > 7:
        return RIDDOR_OVER_7
    if days_off is not None and days_off > 3:
        return RIDDOR_OVER_3
    return ''


# Investigation target: company practice, not law. Each company sets its own (SafetySettings, 1 to 60
# days); 7 is the default until the product owner confirms it.
INVESTIGATE_WITHIN_DAYS = 7


def investigation_due(reported_on, days=INVESTIGATE_WITHIN_DAYS):
    """The day an incident should be investigated by: `days` calendar days after it was reported."""
    return reported_on + datetime.timedelta(days=INVESTIGATE_WITHIN_DAYS if days is None else days)


# --- Visitor sign-in retention (UK GDPR: keep personal data no longer than needed) ---------
# A visitor's QR sign-in (name and company, no account) is deleted this many months after they signed
# in. Each company sets its own, 3 to 36 months; 12 is the default until the product owner confirms it.
# Our staff's and portal users' sign-ins are kept (they are part of the job's records), and incidents
# are never deleted by this.
VISITOR_RETENTION_MONTHS, VISITOR_RETENTION_MIN, VISITOR_RETENTION_MAX = 12, 3, 36


def months_before(moment, months):
    """The same time `months` calendar months earlier, on the last day of the month if that day doesn't exist."""
    import calendar
    total = moment.year * 12 + (moment.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def visitor_purge_cutoff(now, months=VISITOR_RETENTION_MONTHS):
    """Visitor sign-ins from before this moment are deleted. `months` is kept within 3 to 36."""
    months = max(VISITOR_RETENTION_MIN, min(VISITOR_RETENTION_MAX, int(months or VISITOR_RETENTION_MONTHS)))
    return months_before(now, months)


# --- CDM 2015 -------------------------------------------------------------------------------

NOTIFIABLE_WORKING_DAYS = 30
NOTIFIABLE_WORKERS = 20
NOTIFIABLE_PERSON_DAYS = 500
CPP_WARNING_DAYS = 14


def is_notifiable(working_days=None, peak_workers=None, person_days=None):
    """
    True if notifiable to HSE (F10), False if not, None if there isn't enough to say.
    Longer than 30 working days with more than 20 people at once, or more than 500 person-days.
    """
    if person_days is not None and person_days > NOTIFIABLE_PERSON_DAYS:
        return True
    if working_days is not None and peak_workers is not None:
        if working_days > NOTIFIABLE_WORKING_DAYS and peak_workers > NOTIFIABLE_WORKERS:
            return True
        if person_days is not None:
            return False
        # Without a person-day figure, the most it could be is every day at the peak
        if working_days * peak_workers <= NOTIFIABLE_PERSON_DAYS:
            return False
        return None
    return None  # the 30-day / 20-worker test can't be checked yet


def needs_pd_and_pc(contractor_count):
    """The client must appoint a principal designer and principal contractor: more than one contractor."""
    return contractor_count is not None and contractor_count > 1


def cpp_check_due(start_date, today):
    """The date the construction phase plan is chased for, or None if it's too early to chase it."""
    if start_date is None:
        return None
    if today < start_date - datetime.timedelta(days=CPP_WARNING_DAYS):
        return None
    return start_date


# --- Site sign-in --------------------------------------------------------------------------

VISITOR_COOKIE_HOURS = 16


def on_site_since(now_local):
    """Sign-ins from this moment (midnight today, local time) onwards count as on site now."""
    return now_local.replace(hour=0, minute=0, second=0, microsecond=0)
