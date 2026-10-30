"""
Demo data for the commercial backbone, called by `manage.py seed_demo` inside the demo
organisation's org_context, after the Leeds job, its RFIs, variations and sub variations exist.

  Leeds bar job (P-0001-ish): JCT IC 2016, the client as Contract Administrator. VO-001 waits on
      the client (ball in court), VO-003 is linked to RFI-001, and CE-001 ties RFI-001, VO-003
      and the joinery sub variation SV-002 together. CE-002 (extract route) could delay
      completion, so it shows the JCT notice-of-delay prompt.
  Salford Quays office: NEC4 ECC Option A. EW-001 is open with a meeting booked, CE-001 came
      from RFI-001 and became draft VO-001, with the 8-week notification clock nearly out, and
      CE-002 is quoted with the Project Manager's reply late and deemed acceptance running.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from contracts.models import MainContract
from core.workflow import apply_transition
from delivery.models import SubVariation
from projects.models import Project, ProjectMember
from rfis.models import RFI
from variations.models import Variation
from variations.views import sync_rfi_link

from .links import set_source
from .models import ChangeEvent, ChangeEventLine, EarlyWarning
from .views import raise_variation


def _lines(ce, *lines):
    for kind, description, amount in lines:
        ChangeEventLine.objects.create(change_event=ce, kind=kind, description=description, amount=Decimal(amount))


def seed(*, leeds, pm, client):
    today = timezone.localdate()
    COST, REVENUE = ChangeEventLine.KIND_COST, ChangeEventLine.KIND_REVENUE

    # --- Leeds: JCT IC 2016 -----------------------------------------------------------
    ca = ProjectMember.objects.get(project=leeds, user=client)
    leeds_contract = MainContract(project=leeds, form='JCT_IC', edition='2016', administrator=ca,
                                  lads_rate=Decimal('4500'), valuation_day=28,
                                  amendments='Client schedule of amendments rev B: pay less notice 7 days; '
                                             'no fluctuations; design portion for bar joinery only.')
    leeds_contract.apply_preset()
    leeds_contract.pay_less_notice_days = 7  # as amended
    leeds_contract.save()

    rfi1 = RFI.objects.get(project=leeds, title__startswith='Bar back panelling')
    rfi2 = RFI.objects.get(project=leeds, title__startswith='Kitchen extract route')
    for vo in Variation.objects.filter(project=leeds):
        if vo.status == Variation.STATUS_SUBMITTED:
            vo.ball_in_court_user = client
            vo.save(actor=pm, audit_action='reassigned')
        if vo.rfi_id:
            sync_rfi_link(vo, pm)
    veneer_vo = Variation.objects.get(project=leeds, rfi=rfi1)

    ce = ChangeEvent(project=leeds, title='Bar back veneer: smoked eucalyptus', origin='RFI', ball_in_court_user=pm,
                     description='RFI-001 answer awaited. If eucalyptus is confirmed the bar back and front are '
                                 're-veneered (joinery SV-002) and we recover the difference from the client (VO-003).',
                     aware_on=today - timedelta(days=12), notified_on=today - timedelta(days=10))
    ce.save(actor=pm)
    _lines(ce, (COST, 'Lund Joinery re-veneer (SV-002)', '2350'), (COST, 'Site supervision, 2 days', '640'),
           (REVENUE, 'Veneer change to client', '7300'))
    set_source(ce, rfi1, 'rfis.rfi', actor=pm)
    set_source(veneer_vo, ce, 'changes.changeevent', actor=pm)
    apply_transition(ce, 'notify', pm)
    sv = SubVariation.objects.filter(project=leeds, title__startswith='Bar front re-veneer').first()
    if sv:
        sv.change_event = ce
        sv.save(update_fields=['change_event'])

    ce2 = ChangeEvent(project=leeds, title='Extract duct reroute round downstand beam', origin='RFI',
                      ball_in_court_user=pm, time_impact_days=4, aware_on=today - timedelta(days=6),
                      description='Extract duct clashes with the downstand beam on grid C/4 (RFI-002). Rerouting '
                                  'holds up first fix in the kitchen by about four days.')
    ce2.save(actor=pm)
    _lines(ce2, (COST, 'Apex Mechanical: extra bends and supports', '1850'), (REVENUE, 'Reroute to client', '2600'))
    set_source(ce2, rfi2, 'rfis.rfi', actor=pm)

    # --- Salford Quays: NEC4 ECC Option A -------------------------------------------------
    salford = Project.objects.get(name='Office CAT A, Salford Quays')
    ProjectMember.objects.get_or_create(project=salford, user=pm, defaults={'role': ProjectMember.ROLE_PM})
    nec = MainContract(project=salford, form='NEC4_ECC', nec_main_option='A', lads_rate=Decimal('1500'),
                       retention_percent=Decimal('3'), defects_period_months=12, valuation_day=25,
                       amendments='Z1: Contractor design of raised access floor. X7 delay damages, X16 retention.')
    nec.apply_preset()
    nec.save()

    ew = EarlyWarning(project=salford, title='Landlord approval of riser penetrations', ball_in_court_user=pm,
                      description='Landlord has not yet approved core drilling through the riser walls. Without it '
                                  'by the end of next week, first fix on level 4 stops.',
                      raised_by_side=EarlyWarning.SIDE_CONTRACTOR, risk_time=True, risk_cost=True,
                      mitigation='Chase landlord surveyor daily; resequence level 3 first fix to keep the gang busy.',
                      meeting_date=today + timedelta(days=3))
    ew.save(actor=pm)

    rfi = RFI(project=salford, title='Raised floor height at the core', category='DESIGN', ball_in_court_user=pm,
              question='Drawings show a 150mm finished floor zone but the core threshold allows 100mm. Which wins?',
              created_by=pm, status=RFI.STATUS_RESPONDED, response='Use 150mm; ramp the core thresholds.',
              responded_at=timezone.now() - timedelta(days=40), due_date=today - timedelta(days=45))
    rfi.save(actor=pm)

    ce3 = ChangeEvent(project=salford, title='Threshold ramps at the core', origin='RFI', ball_in_court_user=pm,
                      description='RFI-001 answer: keep the 150mm floor and ramp all six core thresholds. Not in the '
                                  'Activity Schedule, so a compensation event (changed Works Information).',
                      aware_on=today - timedelta(days=50))
    ce3.save(actor=pm)
    _lines(ce3, (COST, 'Floor sub: ramps and nosings', '3900'), (REVENUE, 'Quotation for ramps', '5450'))
    set_source(ce3, rfi, 'rfis.rfi', actor=pm)
    raise_variation(ce3, pm)

    ce4 = ChangeEvent(project=salford, title='Extra data points to the trading floor', origin='INSTRUCTION',
                      ball_in_court_user=pm, description="Project Manager's instruction PMI-014: 40 extra data points.",
                      aware_on=today - timedelta(days=40), notified_on=today - timedelta(days=39),
                      quotation_instructed_on=today - timedelta(days=36),
                      quotation_submitted_on=today - timedelta(days=20),
                      failure_notified_on=today - timedelta(days=3))
    ce4.save(actor=pm)
    _lines(ce4, (COST, 'Brightside Electrical', '8200'), (REVENUE, 'Quotation to Project Manager', '10950'))
    apply_transition(ce4, 'notify', pm)
    apply_transition(ce4, 'quote', pm)
    return {'leeds_contract': leeds_contract, 'nec_contract': nec}
