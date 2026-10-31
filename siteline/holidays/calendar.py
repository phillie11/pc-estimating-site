"""
UK bank holidays by region, and Construction Act day counting. ADVISORY: check your contract.

The table is holidays/data/bank-holidays.json, in the shape gov.uk publishes at
https://www.gov.uk/bank-holidays.json. The bundled copy covers 2020-2030. It was entered by
hand from the published dates (the published rules, plus the one-off days: VE Day 2020, the
2022 Platinum Jubilee and state funeral, the 2023 Coronation), because gov.uk couldn't be
reached when it was built. `manage.py update_bank_holidays` replaces it with gov.uk's copy.
gov.uk only publishes about two years ahead, so later years are the regular pattern and a
special bank holiday announced later needs that refresh.

Housing Grants, Construction and Regeneration Act 1996 s116, which also governs the Scheme
for Construction Contracts: a period of days "after or from" a date begins the day after it,
and Christmas Day, Good Friday and bank holidays (in the part of the UK whose law governs
the contract) are left out of the count. Weekends are counted, except a Saturday that is itself a
bank holiday under Schedule 1 of the Banking and Financial Dealings Act 1971 (26 December in
England and Wales and Northern Ireland, 1 and 2 January in Scotland, 17 March in Northern
Ireland; see STATUTORY_UNLESS_SUNDAY). The JCT forms count periods of
days the same way (their "Reckoning periods of days" clause excludes Public Holidays).

NEC4 clocks are counted in calendar weeks and don't use this (changes/clocks.py).
"""
import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / 'data' / 'bank-holidays.json'

ENGLAND_AND_WALES = 'england-and-wales'
SCOTLAND = 'scotland'
NORTHERN_IRELAND = 'northern-ireland'
REGION_CHOICES = [(ENGLAND_AND_WALES, 'England and Wales'), (SCOTLAND, 'Scotland'),
                  (NORTHERN_IRELAND, 'Northern Ireland')]
DEFAULT_REGION = ENGLAND_AND_WALES


def easter_sunday(year):
    """Western Easter (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def good_friday(year):
    return easter_sunday(year) - timedelta(days=2)


def validate(data):
    """Check a gov.uk-shaped table; return {region: frozenset(dates)}. Raises ValueError if it isn't one."""
    out = {}
    for region, _ in REGION_CHOICES:
        try:
            events = data[region]['events']
            out[region] = frozenset(date.fromisoformat(e['date']) for e in events)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f'Not a bank holiday table: {region} is missing or malformed ({exc}).') from exc
        if not out[region]:
            raise ValueError(f'Not a bank holiday table: no dates for {region}.')
    return out


@lru_cache(maxsize=1)
def _table():
    with open(DATA_FILE, encoding='utf-8') as fh:
        return validate(json.load(fh))


def reload():
    """Forget the cached table (after update_bank_holidays writes a new one)."""
    _table.cache_clear()


def bank_holidays(region=DEFAULT_REGION):
    return _table()[region or DEFAULT_REGION]


def covered_years(region=DEFAULT_REGION):
    days = bank_holidays(region)
    return range(min(days).year, max(days).year + 1)


SUNDAY = 6

# Days that are bank holidays by statute even when they fall on a Saturday: Banking and Financial
# Dealings Act 1971, Schedule 1 (para 1 England and Wales, para 2 Scotland, para 3 Northern Ireland).
# gov.uk's table lists only the weekday substitutes (made by proclamation), so a Saturday 26 December
# (England and Wales) or 2 January (Scotland) is missing from it, though s116 leaves it out of the
# count. (month, day): the day is a bank holiday "if it be not a Sunday". Scottish Boxing Day is by
# proclamation only, so there only gov.uk's substitute counts.
STATUTORY_UNLESS_SUNDAY = {
    ENGLAND_AND_WALES: ((12, 26),),
    SCOTLAND: ((1, 1), (1, 2)),
    NORTHERN_IRELAND: ((3, 17), (12, 26)),
}


def is_schedule_1_day(day, region=DEFAULT_REGION):
    """A bank holiday under Sch 1 of the 1971 Act on its own date (any day but Sunday)."""
    return day.weekday() != SUNDAY and (day.month, day.day) in STATUTORY_UNLESS_SUNDAY.get(region or DEFAULT_REGION, ())


def is_excluded(day, region=DEFAULT_REGION):
    """
    Left out of a Construction Act day count: Christmas Day, Good Friday, a bank holiday in the
    region (gov.uk's table, with its substitutes), or a Schedule 1 bank holiday that fell on a Saturday.
    """
    return ((day.month == 12 and day.day == 25) or day == good_friday(day.year) or day in bank_holidays(region)
            or is_schedule_1_day(day, region))


def add_days(start, days, region=DEFAULT_REGION):
    """The date `days` counted days after `start`, leaving out excluded days (s116). 0 days is `start` itself."""
    day, left = start, days
    while left > 0:
        day += timedelta(days=1)
        if not is_excluded(day, region):
            left -= 1
    return day


def subtract_days(end, days, region=DEFAULT_REGION):
    """The latest date that is `days` counted days before `end` ("not later than N days before"), same rule."""
    day, left = end, days
    while left > 0:
        day -= timedelta(days=1)
        if not is_excluded(day, region):
            left -= 1
    return day
