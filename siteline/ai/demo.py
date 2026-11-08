"""
Demo data for AI drafting, called at the end of tenancy's seed_demo inside org_context, after the Kingsway pack
is built (handoverpack/demo.py). Nothing here calls the AI service: the drafts are written out as if it had.

  - AI drafting switched on, £20 a month, for owners, admins and estimators, with an invented company profile
  - the company's own clarifications playbook (version 1), so Settings shows an override
  - five AI-drafted clarifications on the Kingsway pack, not yet approved (so not in the pack or its export)
  - a bid brief on Kingsway read from its three tender documents, one key date not yet accepted
  - two usage rows this month, so the usage page has something to show

Every name and figure is invented.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from projects.models import DocumentRevision, Project

from .models import CLARIFICATIONS, TENDER_BRIEF, AISettings, AIUsage, BidBrief, BriefItem, Playbook

PLAYBOOK = """\
Our clarifications

- One point each, plain English, written for the client's quantity surveyor.
- Always give the quantity and the drawing number from the line. We price to the drawings, not the spec.
- Anything that is our own rate with no subcontract quote: say so and say what it covers, so the delivery team
  knows to buy it out first.
- We never include builder's work for the client's IT or AV contractors: say so whenever data or AV appears.
- Out-of-hours work only where the site rules force it, and then say how many shifts we allowed.
"""

DRAFTS = [
    ('Partitions Ceilings', 'Allowed 186 m2 of metal stud partition with two layers of 15 mm board each side, taken '
     'to the underside of the slab as drawing KW-A-201. A deflection head beyond the standard track is not '
     'included.', 'Partitions Ceilings row 4'),
    ('Partitions Ceilings', 'The 12 access hatches (600 x 600) are priced only by a ceiling contractor we have not '
     'selected. The price assumes the selected contractor supplies them at a similar rate; confirm before order.',
     'Partitions Ceilings row 8'),
    ('Mechanical', 'Comfort cooling to the comms room is not priced (awaiting a price) and is not in the tender '
     'total. It will follow as an addition if required.', 'Mechanical row 6'),
    ('Decoration', 'Decoration of 556 m2 of new partitions (mist coat and two coats emulsion) is our own rate with '
     'no subcontract quote. Existing walls and ceilings are not decorated.', 'Decoration row 4'),
    ('Provisional sums', 'A provisional sum of £5,000 is carried for the landlord\'s works to the riser, as the '
     'client instructed. Work beyond the riser is excluded.', 'Prov Sums row 4'),
]


def seed(owner, estimator):
    kings = Project.objects.get(name='Office CAT B, Kingsway')
    AISettings.objects.update_or_create(organization=kings.organization, defaults={
        'enabled': True, 'monthly_cap': Decimal('20.00'), 'who': AISettings.ESTIMATORS, 'updated_by': owner,
        'profile': 'Commercial fit-out and refurbishment of offices, retail and hospitality, usually £400k to '
                   '£2.5m, within a two-hour drive of the office.',
        'overheads_profit': Decimal('12.50')})
    Playbook.objects.create(task=CLARIFICATIONS, version=1, text=PLAYBOOK, created_by=owner)

    pack = kings.handover_pack
    from handoverpack.models import PackClarification
    for i, (trade, text, basis) in enumerate(DRAFTS):
        PackClarification.objects.create(pack=pack, source=PackClarification.FROM_DRAFT, approved=False, trade=trade,
                                         text=text, basis=basis, order=200000 + i)

    docs = {r.document.reference: r for r in DocumentRevision.objects.filter(
        document__project=kings, is_current=True).select_related('document')}
    spec, ga, rcp = docs.get('KW-SPEC-01'), docs.get('KW-A-100'), docs.get('KW-A-300')
    now = timezone.now()
    brief = BidBrief.objects.create(
        project=kings, recommendation=BidBrief.CAUTION, ai_drafted=True, generated_at=now - timedelta(days=1),
        generated_by=estimator,
        summary='CAT B fit-out of levels 2 and 3 under JCT Intermediate 2024 with amendments, about 14 weeks. '
                'Value range £1.1m to £1.4m. The liquidated damages are high for the size of job, and the '
                'specification names a ceiling system the drawings contradict.',
        value_basis='Desktop estimate from the drawings and specification, no site visit, plus 12.5% overheads '
                    'and profit. No risk or contingency allowance.',
        sources=[{'revision': r.pk, 'label': f'{r.document.reference} {r.document.title} rev {r.revision}',
                  'pages': 1} for r in (spec, ga, rcp) if r])

    def item(section, label, text, rev=None, page=None, i=0, **kw):
        BriefItem.objects.create(brief=brief, section=section, label=label, text=text, ai_drafted=True,
                                 source_revision=rev, source_label=(f'{rev.document.reference} rev {rev.revision}'
                                                                    if rev else ''),
                                 source_page=page, order=BriefItem.SECTIONS.index(section) * 1000 + i, **kw)

    today = timezone.localdate()
    item('date', 'Tender return', 'Return by 12:00 to the client\'s quantity surveyor, electronic copy only.', spec,
         1, 0, date=today + timedelta(days=12), date_kind=BriefItem.RETURN)
    item('date', 'Start on site', 'Possession of both floors on the start date; no phased handover.', spec, 1, 1,
         date=today + timedelta(days=60), date_kind=BriefItem.START)
    item('scope', 'Partitions and ceilings', 'New metal stud partitions, linings and MF ceilings to levels 2 and 3.',
         ga, 1, 0, value_low=Decimal('310000'), value_high=Decimal('380000'))
    item('scope', 'Mechanical and electrical', 'Lighting, small power, fan coil alterations; landlord\'s BMS '
         'interface.', spec, 1, 1, value_low=Decimal('420000'), value_high=Decimal('510000'))
    item('lads', 'Liquidated damages', '£6,500 per week, uncapped.', spec, 1)
    item('retention', 'Retention', '5%, half released at practical completion.', spec, 1)
    item('risk', 'Ceiling system contradiction', 'The specification names a 600 x 600 grid ceiling; the reflected '
         'ceiling plan shows plasterboard MF throughout. Price the drawing and raise a query.', rcp, 1, 0,
         severity='high', rank=1)
    item('risk', 'Uncapped LADs', 'Liquidated damages are uncapped at £6,500 a week on a 14-week job. Ask for a '
         '10% cap.', spec, 1, 1, severity='critical', rank=0)
    item('pack_missing', 'Asbestos survey', 'Refurbishment and demolition survey: not in pack.', None, None)
    item('action', 'Query the ceiling system', 'Send the ceiling query before queries close.', None, None)

    for feature, note, cost, tin, tout in ((CLARIFICATIONS, 'pricing workbook revision 2', '0.21', 9800, 6100),
                                           (TENDER_BRIEF, '3 documents, 3 pages', '0.12', 14200, 2900)):
        AIUsage.objects.create(user=estimator, feature=feature, project=kings, model='claude-opus-5-5',
                               status=AIUsage.OK, input_tokens=tin, output_tokens=tout, cost_gbp=Decimal(cost),
                               note=note, created_at=now - timedelta(days=1))
    return brief
