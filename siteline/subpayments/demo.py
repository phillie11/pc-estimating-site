"""
Demo subcontractor payments, called by `manage.py seed_demo` inside the demo organisation's
org_context, after costs.demo (so the Leeds and Salford orders exist).

  Leeds (project 1)
    Lund Joinery (CIS registered, 20%)   SA-001 paid (our payment notice in time, paid before the
                                         final date); SA-002 received a few days ago, awaiting our
                                         payment notice
    Apex Mechanical (M&E, gross, 0%)     appointed and committed; SA-003 with our payment notice
                                         given, awaiting payment
    Pennine Drylining (unverified, 30%)  SA-004 paid, so its advice shows the 30% deduction
  Salford Quays (project 4)
    Brightside Electrical (registered, reverse charge)
                                         SA-001 with no payment notice from us in time: it stands
                                         as the notified sum (the red warning on the staff side)

Dates are worked out from today so the demo always reads the same way.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from contracts import presets
from core.workflow import apply_transition
from costs import services as cost_services
from costs.models import CostCode
from directory.models import Package, Subcontractor, Trade
from projects.models import Project

from . import rules, services
from .models import SubApplication, SubcontractorTax, settings_for, subcontract_for


def _tax(name, status, number='', **kw):
    sub = Subcontractor.objects.get(company_name=name)
    SubcontractorTax.objects.update_or_create(
        subcontractor=sub, defaults=dict(cis_status=status, verification_number=number,
                                         verified_on=timezone.localdate() - timedelta(days=60) if number else None,
                                         **kw))
    return sub


def _prev_month(day, valuation_day):
    first = day.replace(day=1) - timedelta(days=1)
    return presets.valuation_date_in(first.year, first.month, valuation_day)


def _paid(app, actor, today, materials=None, ref=''):
    apply_transition(app, 'payment_notice', actor, amount=app.amount_due, basis='Valued as applied.',
                     given_on=min(app.payment_notice_by, today))
    app = SubApplication.objects.get(pk=app.pk)
    services.record_payment(app, actor, app.outstanding, min(app.final_date - timedelta(days=2), today),
                            materials=materials, reference=ref)
    return SubApplication.objects.get(pk=app.pk)


def seed(*, leeds, pm, owner):
    today = timezone.localdate()
    settings_for(leeds.organization)
    lund = _tax('Lund Joinery Ltd', rules.CIS_REGISTERED, 'V6629431187', utr='4417709921')
    _tax('Pennine Drylining', rules.CIS_UNVERIFIED)
    _tax('Apex Mechanical', rules.CIS_GROSS, 'V2091186455', utr='8830156624')
    _tax('Brightside Electrical', rules.CIS_REGISTERED, 'V5513098870', reverse_charge=True)

    # A valuation day a few days ago, so Lund's latest application is still inside our payment notice period
    vday = min((today - timedelta(days=3)).day, 28)

    # --- Leeds: Lund Joinery ------------------------------------------------------------
    join = Package.objects.get(project=leeds, subcontractor=lund, status=Package.APPOINTED)
    sc = subcontract_for(join, pm)
    sc.valuation_day = vday
    sc.save()
    v2, _ = sc.around(today)
    v1 = _prev_month(v2, vday)
    a1 = services.receive_application(sc, lund_user(leeds), v1, Decimal('58000'), Decimal('21000'), by_sub=True,
                                      received_on=v1, notes='Bar joinery manufacture: carcasses and frames.')
    _paid(a1, pm, today, ref='BACS 40117')
    services.receive_application(sc, lund_user(leeds), v2, Decimal('104500'), Decimal('14750'), by_sub=True,
                                 received_on=v2, notes='Bar back panelling delivered; install 40% complete.')

    # --- Leeds: Apex Mechanical (M&E, gross payment status) ------------------------------
    apex = Subcontractor.objects.get(company_name='Apex Mechanical')
    trade = Trade.objects.filter(name='Mechanical').first()
    mech = Package.objects.create(project=leeds, subcontractor=apex, trade=trade, status=Package.APPOINTED,
                                  quote_value=Decimal('152000'), decided_at=timezone.now() - timedelta(days=35))
    cost_services.commit_package(mech, CostCode.objects.get(code='240'), pm)
    sc_m = subcontract_for(mech, pm)
    sc_m.valuation_day = vday
    sc_m.save()
    a3 = services.receive_application(sc_m, pm, v2, Decimal('46800'), Decimal('18200'), received_on=v2,
                                      notes='Sent by email: first fix ventilation and kitchen extract.')
    apply_transition(a3, 'payment_notice', pm, amount=a3.amount_due - Decimal('1900.00'),
                     basis='Kitchen extract fan not yet commissioned: valued at 50%, not 100%.',
                     given_on=min(a3.payment_notice_by, today))

    # --- Leeds: Pennine Drylining (unverified, 30%) --------------------------------------
    pennine = Subcontractor.objects.get(company_name='Pennine Drylining')
    dry = Package.objects.get(project=leeds, subcontractor=pennine, status=Package.APPOINTED)
    sc_d = subcontract_for(dry, pm)
    sc_d.valuation_day = vday
    sc_d.save()
    a4 = services.receive_application(sc_d, pm, v1, Decimal('31400'), Decimal('9800'), received_on=v1)
    _paid(a4, pm, today, ref='BACS 40121')

    # --- Salford: Brightside Electrical, our notice missed ------------------------------
    salford = Project.objects.get(name='Office CAT A, Salford Quays')
    bright = Subcontractor.objects.get(company_name='Brightside Electrical')
    elec = Package.objects.get(project=salford, subcontractor=bright, status=Package.APPOINTED)
    sc_e = subcontract_for(elec, pm)
    v = today - timedelta(days=20)
    sc_e.cycle_kind = rules.CYCLE_DATES  # valuation dates listed in the subcontract
    sc_e.valuation_dates = [(v - timedelta(days=28)).isoformat(), v.isoformat(), (v + timedelta(days=28)).isoformat()]
    sc_e.save()
    services.receive_application(sc_e, owner, v, Decimal('38250'), Decimal('12600'), received_on=v,
                                 notes='Containment and first fix to level 3. Sent by email.')


def lund_user(project):
    from projects.models import ProjectMember
    m = ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                     subcontractor__company_name='Lund Joinery Ltd').select_related('user').first()
    return m.user if m else None
