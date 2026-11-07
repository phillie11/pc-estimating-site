"""
Demo CVRs and daywork sheets, called by `manage.py seed_demo` inside the demo organisation's
org_context, after payments, costs and sub payments, so the figures have something to read.

  Leeds bar job (project 1, JCT IC)
    Daywork rates from the Contract Particulars: joiner, labourer, electrician, scissor lift, tower,
    with 80% on labour, 15% on materials and 10% on plant.
      DW-001  signed by the client's representative and on the change register (a change event)
      DW-002  signed, waiting to go on the change register (on the PM's Needs me)
      DW-003  written today, ready for the client to sign on the phone
      DW-004  written yesterday and sent to the client (client@demo.test) to sign in their portal:
              on their Needs me under Waiting on you
    CVR for the month before last: signed off by the owner, with the costs from before the job
      moved to Siteline as an accrual.
    CVR for last month: submitted by the PM with an accrual and the client's under-valuation of the
      bar back, waiting for the owner's sign-off (on the owner's Needs me).
  Salford Quays (project 4, NEC4 Option A)
    CVR for last month: being prepared, with an accrual (on the PM's Needs me until submitted).
"""
import io
from datetime import timedelta
from decimal import Decimal as D

from django.core.files.base import ContentFile
from django.utils import timezone

from core.workflow import apply_transition
from projects.models import Project

from . import rules, services
from .models import DayworkLine, DayworkRate, DayworkTerms


def signature_png():
    """A made-up squiggle, as a finger would draw it."""
    from PIL import Image, ImageDraw
    img = Image.new('RGBA', (600, 220), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.line([(40, 150), (80, 70), (110, 160), (150, 80), (190, 150), (230, 110), (300, 120), (360, 90), (420, 140),
            (470, 100), (540, 130)], fill=(21, 25, 31, 255), width=5, joint='curve')
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def _sheet(project, actor, rates, title, day, location, instructed, lines):
    sheet = services.new_sheet(project, actor, title=title, work_date=day, location=location, instructed_by=instructed)
    for kind, rate_name, desc, qty, unit, rate in lines:
        r = rates.get(rate_name)
        DayworkLine.objects.create(sheet=sheet, kind=kind, description=desc or r.name, quantity=D(qty),
                                   unit=unit or ('hours' if kind != 'MATERIALS' else ''),
                                   unit_rate=r.rate if r else D(rate), rate=r, created_by=actor)
    return sheet


def _sign(sheet, actor, signer):
    apply_transition(sheet, 'sign', actor, note=f'Signed by {signer}',
                     signature=ContentFile(signature_png(), name='signature.png'), signer=signer, role='CLIENT')
    return sheet


def seed(*, leeds, pm, owner):
    today = timezone.localdate()
    m1 = rules.add_months(today, -1)
    m2 = rules.add_months(today, -2)

    # --- Leeds dayworks ------------------------------------------------------------------
    DayworkTerms.objects.create(project=leeds, labour_pct=D('80'), materials_pct=D('15'), plant_pct=D('10'),
                                basis='Contract Particulars: percentage additions on the Definition of Prime Cost of '
                                      'Daywork', updated_by=pm)
    rates = {}
    for kind, name, rate in [('LABOUR', 'Joiner', '42.50'), ('LABOUR', 'Labourer', '28.00'),
                             ('LABOUR', 'Electrician', '48.00'), ('PLANT', 'Scissor lift', '15.00'),
                             ('PLANT', 'Mobile tower', '6.50')]:
        rates[name] = DayworkRate.objects.create(project=leeds, kind=kind, name=name, rate=D(rate))
    dw1 = _sheet(leeds, pm, rates, 'Move bar servery 600mm after client walk-round', today - timedelta(days=8),
                 'Ground floor bar', "Client's representative on site, verbal; confirmed by email", [
                     ('LABOUR', 'Joiner', '', '16', '', None), ('LABOUR', 'Labourer', '', '8', '', None),
                     ('MATERIALS', None, '18mm MDF sheets', '4', 'sheets', '24.60'),
                     ('MATERIALS', None, 'Fixings and adhesive', '1', 'lot', '38.25'),
                     ('PLANT', 'Mobile tower', '', '8', '', None)])
    _sign(dw1, pm, 'Site representative, Neon Leisure')
    services.send_to_change(dw1, pm)
    dw2 = _sheet(leeds, pm, rates, 'Extra sockets behind the back bar', today - timedelta(days=3), 'Back bar',
                 'Interior designer site instruction SI-07', [
                     ('LABOUR', 'Electrician', '', '6', '', None),
                     ('MATERIALS', None, 'Double sockets and back boxes', '6', 'nr', '11.40'),
                     ('PLANT', 'Scissor lift', '', '3', '', None)])
    _sign(dw2, pm, 'Site representative, Neon Leisure')
    _sheet(leeds, pm, rates, 'Make good to corridor C1 after client move-in damage', today, 'Corridor C1',
           'Client asked on site this morning', [
               ('LABOUR', 'Joiner', '', '3.5', '', None), ('LABOUR', 'Labourer', '', '3.5', '', None),
               ('MATERIALS', None, 'Skirting, primed softwood', '6', 'm', '7.85')])
    # Sent to the client to sign in their portal (cvr/portal.py); set directly so the seed emails nobody
    from projects.models import ProjectMember
    client = ProjectMember.objects.filter(project=leeds, role=ProjectMember.ROLE_CLIENT).select_related('user') \
        .order_by('pk').first()
    if client is not None:
        dw4 = _sheet(leeds, pm, rates, 'Box in exposed pipework in the customer WCs', today - timedelta(days=1),
                     'Customer WCs', "Client's operations manager by email", [
                         ('LABOUR', 'Joiner', '', '5', '', None),
                         ('MATERIALS', None, '12mm moisture-resistant MDF', '2', 'sheets', '31.20')])
        dw4.sign_requested_from, dw4.sign_requested_by = client.user, pm
        dw4.sign_requested_at = timezone.now() - timedelta(hours=20)
        dw4.save(actor=pm, audit_action='signature requested', audit_note='Asked the client to sign in the portal')

    # --- Leeds CVRs ------------------------------------------------------------------------
    from .figures import live_figures
    now_cost = live_figures([leeds], today)[leeds.pk].forecast_cost_base

    def budget_gap(month):
        """The demo's budget was entered after these months: the forecast from it goes in by hand."""
        return now_cost - live_figures([leeds], month)[leeds.pk].forecast_cost_base

    sep, _ = services.start_period(leeds, m2, pm, today)
    services.add_adjustment(sep, 'FINAL_COST', budget_gap(m2), 'Forecast from the tender build-up: the budget went '
                                                               'on Siteline later', pm)
    services.add_adjustment(sep, 'COST', D('148500'), 'Costs to date from the old cost system, before the job moved '
                                                         'to Siteline', pm)
    sep.commentary = 'Strip out and first fix to plan. Costs brought over from the old system as one accrual.'
    sep.save(actor=pm)
    apply_transition(sep, 'submit', pm, today=today)
    apply_transition(sep, 'sign_off', owner, note='Agreed at the monthly review')

    last, _ = services.start_period(leeds, m1, pm, today)
    services.add_adjustment(last, 'COST', D('148500'), 'Costs before the job moved to Siteline (carried)', pm)
    services.add_adjustment(last, 'COST', D('6200'), 'Pennine Drylining October work not yet applied for', pm)
    services.add_adjustment(last, 'VALUE', D('-4250'), "Client's QS valued the bar back at 60%, not 75% (RFI-001)",
                            pm)
    services.add_adjustment(last, 'FINAL_COST', budget_gap(m1), 'Forecast from the tender build-up: the budget went '
                                                                'on Siteline later', pm)
    services.add_adjustment(last, 'FINAL_COST', D('7500'), 'Risk: bar back veneer change may need a new panel run',
                            pm)
    last.commentary = ('Margin to date held. The client valued the bar back under while the veneer is confirmed; we '
                       'expect it back next month. Risk allowance added for a possible second panel run.')
    last.save(actor=pm)
    apply_transition(last, 'submit', pm, today=today)

    # --- Salford Quays ------------------------------------------------------------------------
    salford = Project.objects.get(name='Office CAT A, Salford Quays')
    sal, _ = services.start_period(salford, m1, pm, today)
    services.add_adjustment(sal, 'COST', D('3400'), 'Welfare cabins: October hire invoice not yet received', pm)
    return {'leeds_dayworks': [dw1, dw2], 'leeds_cvr': [sep, last], 'salford_cvr': [sal]}
