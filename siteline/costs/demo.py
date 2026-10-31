"""
Demo cost control, called by `manage.py seed_demo` inside the demo organisation's org_context,
after the commercial demo (changes/demo.py), so packages, sub variations and contracts exist.

  Leeds bar job (project 1): budget from the £1.24m contract less 12%, then £5k moved from
      contingency to joinery. Lund Joinery (with SV-001 added) and Pennine Drylining committed,
      two supplier orders, a few invoices and timesheets, and one invoice posted to the wrong code
      then reversed. Joinery is overspent; the job's forecast margin stays above the 10% warning.
  Salford Quays office (project 4): budget from £1.65m less 11%, three appointed packages
      committed, and a forecast set by hand on mechanical after M&E coordination, which pushes the
      forecast margin under 10%: red on the report and on the PM's and owner's Needs me.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.utils import timezone

from delivery.models import SubVariation
from directory.models import Package, Subcontractor, Trade
from projects.models import Project

from . import ledger, services
from .access import settings_for
from .models import CostCode
from .money import budget_from_tender


def _budget(project, actor, margin_pct, amounts, note):
    codes = {c.code: c for c in CostCode.objects.all()}
    target = budget_from_tender(project.value, margin_pct)
    lines = [(codes[k], '', Decimal(v)) for k, v in amounts.items()]
    lines.append((codes['900'], 'Contingency and risk allowance', target - sum(a for _, _, a in lines)))
    services.set_original_budget(project, actor, project.value, Decimal(margin_pct), lines, reason=note)
    return codes


def seed(*, leeds, pm, owner, demo_pdf=None):
    settings_for(leeds.organization)
    today = timezone.localdate()

    # --- Leeds -------------------------------------------------------------------------
    codes = _budget(leeds, pm, '12', {
        '100': 96000, '110': 18500, '120': 14000, '130': 12500, '140': 6200, '150': 9000, '200': 42000,
        '210': 98000, '215': 46000, '220': 175000, '230': 58000, '235': 32000, '240': 165000, '250': 140000,
        '255': 18000, '300': 38000, '400': 24000, '500': 9000, '600': 22000, '610': 4500,
    }, 'From the tender build-up at handover.')
    services.transfer_budget(leeds, pm, codes['900'], codes['220'], Decimal('5000'),
                             'Bar back veneer upgrade after RFI-001: allowance moved from contingency.')
    join = Package.objects.get(project=leeds, status=Package.APPOINTED, trade__name='Joinery')
    dry = Package.objects.get(project=leeds, status=Package.APPOINTED, trade__name='Drylining & partitions')
    join_order = services.commit_package(join, codes['220'], pm)
    services.commit_package(dry, codes['210'], pm)
    for sv in SubVariation.objects.filter(package=join, status=SubVariation.APPROVED):
        services.add_sub_variation(join_order, sv, pm)
    plant = services.place_order(leeds, pm, codes['500'], 'Speedy Hire', Decimal('6800'), reference='PO-1043',
                                 description='Scissor lift and towers, 8 weeks', order_date=today - timedelta(days=26))
    mats = services.place_order(leeds, pm, codes['400'], 'Travis Perkins', Decimal('11250'), reference='PO-1047',
                                description='Timber, fixings and sundries', order_date=today - timedelta(days=21))
    # Lund Joinery's valuations are paid through Sub payments (subpayments/demo.py), which posts
    # their actual cost, so no invoice is entered here against their order (it would count twice).
    inv = ContentFile(demo_pdf('Invoice SH-55120', 'Speedy Hire'), name='speedy-55120.pdf') if demo_pdf else None
    services.record_actual(leeds, pm, codes['500'], 'INVOICE', 'Speedy Hire', Decimal('2150'), reference='SH-55120',
                           cost_date=today - timedelta(days=12), commitment=plant, file=inv)
    services.record_actual(leeds, pm, codes['400'], 'INVOICE', 'Travis Perkins', Decimal('4380.12'),
                           reference='TP-771904', cost_date=today - timedelta(days=9), commitment=mats)
    services.record_actual(leeds, pm, codes['300'], 'TIMESHEET', 'Site joiners (2)', Decimal('8640'),
                           reference='Weeks 1-3', cost_date=today - timedelta(days=7), description='Direct labour')
    services.record_actual(leeds, pm, codes['100'], 'TIMESHEET', 'Site manager', Decimal('24000'),
                           reference='Month 1', cost_date=today - timedelta(days=2))
    wrong = services.record_actual(leeds, pm, codes['500'], 'INVOICE', 'Leeds Skip Hire', Decimal('1960'),
                                   reference='LSH-3302', cost_date=today - timedelta(days=6))
    entry = ledger.entries(leeds).get(source_id=wrong.pk, kind=ledger.ACTUAL)
    ledger.reverse(entry, pm, 'Skips are waste, not plant: posted to the wrong cost code')
    services.record_actual(leeds, pm, codes['130'], 'INVOICE', 'Leeds Skip Hire', Decimal('1960'),
                           reference='LSH-3302', cost_date=today - timedelta(days=6))

    # --- Salford Quays -----------------------------------------------------------------
    salford = Project.objects.get(name='Office CAT A, Salford Quays')
    codes = _budget(salford, pm, '11', {
        '100': 120000, '110': 26000, '120': 18000, '130': 14000, '140': 9000, '150': 12000, '210': 205000,
        '215': 120000, '220': 90000, '230': 140000, '235': 41000, '240': 260000, '250': 215000, '255': 22000,
        '300': 30000, '400': 28000, '500': 14000, '600': 38000, '610': 6500,
    }, 'From the tender build-up at handover.')
    trades = {t.name: t for t in Trade.objects.all()}
    for name, trade, value, code in [('Pennine Drylining', 'Drylining & partitions', '199800', '210'),
                                     ('Brightside Electrical', 'Electrical', '208400', '250'),
                                     ('Apex Mechanical', 'Mechanical', '258500', '240')]:
        p = Package.objects.create(project=salford, subcontractor=Subcontractor.objects.get(company_name=name),
                                   trade=trades[trade], status=Package.APPOINTED, quote_value=Decimal(value),
                                   decided_at=timezone.now() - timedelta(days=20))
        services.commit_package(p, codes[code], pm)
    services.record_actual(salford, pm, codes['100'], 'TIMESHEET', 'Site manager', Decimal('9200'),
                           reference='Weeks 1-2', cost_date=today - timedelta(days=1))
    services.record_actual(salford, pm, codes['110'], 'INVOICE', 'Portable Cabins North', Decimal('4850'),
                           reference='PCN-20419', cost_date=today - timedelta(days=8), description='Welfare set-up')
    services.set_forecast(salford, pm, codes['240'], Decimal('300000'),
                          'M&E coordination added four VRF units on level 3; Apex pricing the change.')
