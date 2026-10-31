"""
Demo applications for payment, called by `manage.py seed_demo` inside the demo organisation's
org_context, after changes.demo has set up the contracts (Leeds: JCT IC 2016, 5% retention,
valuation day 28; Salford Quays: NEC4 Option A, 3% retention, assessment day 25).

  Leeds   AP-001  paid in full (payment notice in time, paid before the final date)
          AP-002  certified (the client's payment notice, a little under the application) and
                  awaiting payment
          AP-003  draft for the next valuation date, work done entered by section
  Salford AP-001  submitted, waiting for the Project Manager's payment certificate (no client
                  is on that job in the demo, so nobody was notified)

Dates are worked out from today so the demo always reads the same way.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from contracts import presets
from contracts.models import MainContract
from core.workflow import apply_transition
from projects.models import Project

from . import rules
from .models import Application, ApplicationLine, Payment


def _valuation_dates(day, today):
    """The last valuation date whose final date for payment is still ahead (else the latest past one), and its neighbours."""
    months = []
    y, m = today.year, today.month
    for _ in range(4):
        months.append(presets.valuation_date_in(y, m, day))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return [d for d in months if d < today]


def _make(project, actor, valuation, work_done, mos=Decimal('0'), notes=''):
    contract = MainContract.objects.filter(project=project).first()
    app = Application(project=project, valuation_date=valuation, work_done=Decimal(work_done),
                      materials_on_site=Decimal(mos), notes=notes,
                      title=f'Valuation to {valuation.day} {valuation:%b %Y}')
    app.recalculate(contract)
    app.save(actor=actor)
    return app, contract


def _submit(app, actor, on):
    apply_transition(app, 'submit', actor)
    # Backdate to when it was really sent: the cycle follows the valuation date, not today
    Application.objects.filter(pk=app.pk).update(submitted_at=timezone.make_aware(
        datetime.combine(on, time(10, 0))))
    app.refresh_from_db()
    return app


def seed(*, leeds, pm, client):
    today = timezone.localdate()

    # --- Leeds: JCT IC 2016 -------------------------------------------------------------
    leeds_contract = MainContract.objects.get(project=leeds)
    terms = rules.terms_for(leeds_contract)
    past = _valuation_dates(leeds_contract.valuation_day, today)
    # AP-002's cycle should still be running today (certified, not yet overdue)
    v2 = next((d for d in past if rules.payment_cycle(d, terms).final_date >= today), past[0])
    v1 = past[past.index(v2) + 1]

    ap1, _ = _make(leeds, pm, v1, '182500', '12000',
                   notes='Strip out complete; first fix services and drylining to the ground floor bar.')
    _submit(ap1, pm, v1 + timedelta(days=1))
    apply_transition(ap1, 'payment_notice', client, note='Agreed as applied', amount=ap1.amount_due,
                     basis='Valuation agreed as applied.', given_on=min(ap1.payment_notice_by, today))
    paid_on = min(ap1.final_date - timedelta(days=2), today)
    Payment.objects.create(application=ap1, amount=ap1.notified_total, received_on=paid_on, reference='BACS NLG-4471',
                           recorded_by=pm)
    apply_transition(ap1, 'paid', pm, note='Paid in full', paid_on=paid_on)

    ap2, _ = _make(leeds, pm, v2, '418750', '18500',
                   notes='Second fix in the bar, kitchen first fix, bar joinery in manufacture (materials off site '
                         'not claimed).')
    _submit(ap2, pm, v2 + timedelta(days=1))
    apply_transition(ap2, 'payment_notice', client, amount=ap2.amount_due - Decimal('4250.00'),
                     basis='Bar back joinery valued at 60% complete, not 75%: the veneer is still to be confirmed '
                           '(RFI-001). Everything else as applied.',
                     given_on=min(ap2.due_on, today))

    nxt = presets.next_valuation_date(today, leeds_contract.valuation_day)
    if nxt <= v2:
        nxt = presets.next_valuation_date(v2 + timedelta(days=1), leeds_contract.valuation_day)
    ap3, contract = _make(leeds, pm, nxt, '0', '9500')
    for i, (section, value) in enumerate([('Strip out and soft demolition', '38000'),
                                          ('Drylining, partitions and ceilings', '196400'),
                                          ('Mechanical and electrical', '228900'),
                                          ('Joinery and bar', '121300'),
                                          ('Kitchen extract and services', '54750')]):
        ApplicationLine.objects.create(application=ap3, section=section, value=Decimal(value), position=i)
    ap3.recalculate(contract)
    ap3.save(actor=pm)

    # --- Salford Quays: NEC4 ECC Option A ---------------------------------------------------
    salford = Project.objects.get(name='Office CAT A, Salford Quays')
    nec = MainContract.objects.get(project=salford)
    assessment = _valuation_dates(nec.valuation_day, today)[0]
    s1, _ = _make(salford, pm, assessment, '214600',
                  notes='Activities 1.1 to 2.4 complete (strip out, raised floor to levels 3 and 4).')
    _submit(s1, pm, assessment)
    return {'leeds': [ap1, ap2, ap3], 'salford': [s1]}
