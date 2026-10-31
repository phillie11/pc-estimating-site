"""
Demo data for quote levelling, called at the end of tenancy's seed_demo inside org_context:
Kingsway's joinery levelled across three subcontractors, with an exclusion priced in by us, one
accepted, one still unpriced (the risk warning), and a missing item plugged at the average of the
others. Lund Joinery (sub@demo.test) is still to quote, so the portal demo keeps its quote form.
Nobody is carried yet: carrying is part of the walk-through.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from directory.models import Package, Subcontractor, Trade
from projects.models import Project

from . import services
from .models import Adjustment, Exclusion

LINES = [
    ('J1', 'Reception desk, bespoke, oak veneer with solid surface top', None, 'Item'),
    ('J2', 'Tea point base units, white gloss, soft-close', Decimal('8'), 'm'),
    ('J3', 'Tea point wall units', Decimal('6'), 'm'),
    ('J4', 'Tea point worktop, solid surface, 600 deep', Decimal('8'), 'm'),
    ('J5', 'Meeting room storage walls, full height, oak veneer doors', Decimal('24'), 'm'),
    ('J6', 'Window boards, MDF, painted', Decimal('42'), 'm'),
    ('J7', 'Ironmongery to storage doors', None, 'Item'),
]
# Per line: (rate, sum, included) or None for not priced
PRICES = {
    'Oakline Joinery': [(None, '14500', False), ('520', None, False), ('410', None, False), ('285', None, False),
                        ('690', None, False), ('38', None, False), (None, '1850', False)],
    'Kensal Interiors': [(None, '13800', False), ('495', None, False), ('395', None, False), ('310', None, False),
                         ('655', None, False), None, (None, None, True)],
    'Thameside Joinery': [(None, '15200', False), ('540', None, False), ('430', None, False), ('295', None, False),
                          ('640', None, False), ('41', None, False), (None, '1600', False)],
}
QUOTES = {'Oakline Joinery': '43406.00', 'Kensal Interiors': '38330.00', 'Thameside Joinery': '43142.00'}
EXCLUSIONS = {
    'Oakline Joinery': 'Deliveries outside 08:00-18:00.',
    'Kensal Interiors': 'Protection of finished joinery after handover to follow-on trades.',
    'Thameside Joinery': 'Fire-rated doors to the storage walls (FD30 shown on KW-A-300).',
}


def seed(estimator):
    now, today = timezone.now(), timezone.localdate()
    kings = Project.objects.get(name='Office CAT B, Kingsway')
    joinery = Trade.objects.get(name='Joinery')
    scope = Package.objects.filter(project=kings, trade=joinery).first().scope
    packages = {}
    for name in PRICES:
        sub = Subcontractor.objects.create(company_name=name, status=Subcontractor.STATUS_APPROVED, rating=4,
                                           email=f'tenders@{name.split()[0].lower()}.example',
                                           public_liability_expiry=today + timedelta(days=240),
                                           employers_liability_expiry=today + timedelta(days=240),
                                           region_codes=['LDN'] if name != 'Oakline Joinery' else ['SE', 'LDN'])
        sub.trades.set([joinery])
        packages[name] = Package.objects.create(
            project=kings, subcontractor=sub, trade=joinery, scope=scope, quote_due=kings.quotes_due,
            status=Package.QUOTED, invited_by=estimator, sent_at=now - timedelta(days=12),
            viewed_at=now - timedelta(days=11), quote_value=Decimal(QUOTES[name]), submitted_at=now - timedelta(days=3),
            valid_until=today + timedelta(days=60), programme_weeks=6, exclusions=EXCLUSIONS[name])

    sheet = services.ensure_saved(services.sheet_for(kings, joinery))
    lines = [services.add_line(sheet, ref, desc, qty, unit) for ref, desc, qty, unit in LINES]
    for name, prices in PRICES.items():
        for line, price in zip(lines, prices):
            if price is not None:
                rate, amount, included = price
                services.set_cell(line, packages[name], rate=rate, amount=amount, included=included)

    # Exclusions: each subcontractor's own, brought in from their quote, then dealt with (or not)
    for name, pkg in packages.items():
        services.copy_exclusions(sheet, pkg, estimator)
    kensal = Exclusion.objects.get(sheet=sheet, package=packages['Kensal Interiors'])
    kensal.status, kensal.amount = Exclusion.PRICED, Decimal('650.00')
    services.save_exclusion(kensal, estimator)
    oak = Exclusion.objects.get(sheet=sheet, package=packages['Oakline Joinery'])
    oak.status = Exclusion.ACCEPTED
    services.save_exclusion(oak, estimator)
    # Thameside's fire doors stay unpriced: the risk warning

    # Kensal left the window boards out: plugged at the average of the other two
    services.plug(sheet, lines[5], packages['Kensal Interiors'], Adjustment.AVERAGE, estimator)
    return sheet
