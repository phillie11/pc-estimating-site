"""
Refresh the bundled UK bank holiday table from gov.uk.

    python manage.py update_bank_holidays             # fetch, check, write holidays/data/bank-holidays.json
    python manage.py update_bank_holidays --dry-run   # fetch and check only

The new table is checked (all three regions present, every date valid) before the old one is
replaced. Dates gov.uk no longer lists (it drops past years) are kept, so the file only grows.
Run it once a year, and whenever a special bank holiday is announced.
"""
import json
import os
import tempfile
import urllib.request

from django.core.management.base import BaseCommand, CommandError

from holidays import calendar

URL = 'https://www.gov.uk/bank-holidays.json'


def fetch(url=URL, timeout=20):
    req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'Siteline bank holidays'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        return json.loads(resp.read().decode('utf-8'))


def merge(old, new):
    """gov.uk's table with any older dates from ours that it no longer lists."""
    out = {}
    for region, _ in calendar.REGION_CHOICES:
        events = {e['date']: e for e in old.get(region, {}).get('events', [])}
        events.update({e['date']: e for e in new[region]['events']})
        out[region] = {'division': region, 'events': [events[d] for d in sorted(events)]}
    return out


class Command(BaseCommand):
    help = 'Refresh the UK bank holiday table from gov.uk'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--url', default=URL)

    def handle(self, *args, dry_run=False, url=URL, **opts):
        try:
            new = fetch(url)
            calendar.validate(new)
        except (OSError, ValueError) as e:
            raise CommandError(f'Could not get a bank holiday table from {url}: {e}') from e
        with open(calendar.DATA_FILE, encoding='utf-8') as fh:
            old = json.load(fh)
        merged = merge(old, new)
        calendar.validate(merged)
        added = sum(len(merged[r]['events']) - len(old.get(r, {}).get('events', [])) for r, _ in calendar.REGION_CHOICES)
        if dry_run:
            self.stdout.write(f'Fetched {url}: {added} new date(s). Nothing written (--dry-run).')
            return
        fd, tmp = tempfile.mkstemp(dir=calendar.DATA_FILE.parent, suffix='.json')
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(merged, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, calendar.DATA_FILE)
        calendar.reload()
        self.stdout.write(self.style.SUCCESS(f'Bank holidays updated from {url}: {added} new date(s).'))
