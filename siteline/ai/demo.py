"""
Demo data for AI drafting, called at the end of tenancy's seed_demo inside org_context, after the Kingsway pack
is built (handoverpack/demo.py). Nothing here calls the AI service: the drafts are written out as if it had.

  - AI drafting switched on, £20 a month, for owners, admins and estimators, with an invented company profile
  - the company's own clarifications playbook (version 1), so Settings shows an override
  - five AI-drafted clarifications on the Kingsway pack, not yet approved (so not in the pack or its export)
  - a bid brief on Kingsway read from its three tender documents, one key date not yet accepted
  - two usage rows this month, so the usage page has something to show
  - two AI-drafted RFIs on Kingsway, internal drafts not yet issued
  - an AI mapping of a revised Kensal Interiors joinery quote (rev B, attached) onto the Kingsway levelling, still
    to check. The quote value in Siteline is still their first one, so the page shows the two totals differ; it
    prices the window boards they left out first time, and is £150.00 off their total, explained in a note

Every name and figure is invented.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from projects.models import DocumentRevision, Project

from .models import (CLARIFICATIONS, QUOTE_MAPPING, RFI_DRAFTING, TENDER_BRIEF, AISettings, AIUsage, BidBrief,
                     BriefItem, Playbook, QuoteMapDraft, QuoteMapExclusion, QuoteMapLine)

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
    seed_quote_map(kings, estimator, now)
    seed_rfis(kings, estimator, spec, rcp)
    return brief


def seed_rfis(kings, estimator, spec, rcp):
    """Two AI-drafted RFIs on Kingsway, internal drafts waiting to be checked and issued."""
    from rfis.models import RFI
    for title, question, why, category, priority, rev, page in (
        ('Ceiling system: grid or plasterboard?',
         'The specification names a 600 x 600 grid ceiling, but reflected ceiling plan KW-A-300 shows plasterboard MF '
         'throughout levels 2 and 3. We have priced the plasterboard shown on the drawing. Which is correct?',
         'Changes the ceiling rate on about 410 m2 and the decorating allowance.', 'DRAWING', 'HIGH', rcp, 1),
        ('Fire rating of the core partitions',
         'The specification calls for fire-rated partitions to the core but gives no rating, and KW-A-100 shows no '
         'fire line. We have allowed 60 minutes. What rating is required, and where does it run?',
         'A 90 or 120 minute rating changes the partition build-up and board quantities.', 'SPECIFICATION',
         'NORMAL', spec, 1),
    ):
        r = RFI(project=kings, title=title, question=question, category=category, priority=priority,
                status=RFI.STATUS_DRAFT, visibility=RFI.VIS_INTERNAL, ai_drafted=True, ai_basis=why,
                document_reference=(f'{rev.document.reference} {rev.document.title} rev {rev.revision}, page {page}'
                                    if rev else ''))
        r.save(actor=estimator)
    AIUsage.objects.create(user=estimator, feature=RFI_DRAFTING, project=kings, model='claude-opus-5-5',
                           status=AIUsage.OK, input_tokens=15800, output_tokens=4100, cost_gbp=Decimal('0.11'),
                           note='3 documents, 15,800 tokens held', created_at=timezone.now() - timedelta(hours=2))


# Kensal's revised quote: (line ref, rate, sum, included, source, note)
KENSAL_REV_B = [
    ('J1', None, '13800', False, 'Page 1, item 1', ''),
    ('J2', '495', None, False, 'Page 1, item 2.1', ''),
    ('J3', '395', None, False, 'Page 1, item 2.2', ''),
    ('J4', '310', None, False, 'Page 1, item 2.3', 'They measure 8.5 m: £2,630.00 on their quote; our 8 m gives '
                                                    '£2,480.00. That is the £150.00 difference.'),
    ('J5', '655', None, False, 'Page 2, item 3', ''),
    ('J6', '39', None, False, 'Page 2, item 4', 'New in this revision: left out of their first quote.'),
    ('J7', None, None, True, 'Page 2, item 3', 'Their item 3 says ironmongery is included in the storage walls.'),
]


def seed_quote_map(kings, estimator, now):
    from directory.models import Package
    from levelling.models import LevellingSheet
    sheet = LevellingSheet.objects.filter(project=kings, trade__name='Joinery').first()
    package = Package.objects.filter(project=kings, subcontractor__company_name='Kensal Interiors').first()
    if sheet is None or package is None:
        return None
    lines = {ln.ref: ln for ln in sheet.lines.all()}
    # Their revised quote, attached to the package, so the draft matches the file (quotemap.stale)
    import hashlib

    from django.core.files.base import ContentFile
    data = (b'%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n'
            b'% Demo quote: Kensal Interiors joinery, revision B (invented)\n%%EOF\n')
    package.quote_file.save('kensal-joinery-quote-rev-b.pdf', ContentFile(data), save=True)
    draft = QuoteMapDraft.objects.create(
        quote_name=package.quote_file.name, quote_size=len(data), quote_sha256=hashlib.sha256(data).hexdigest(),
        project=kings, sheet=sheet, package=package, created_by=estimator, source_label='Kensal joinery quote rev B.pdf',
        quote_total=Decimal('40438.00'), total_source='Page 2, summary', model_ties=False,
        model_difference=Decimal('150.00'),
        unmatched=[{'text': 'Optional: soft-close upgrade to the wall units', 'amount': '320.00',
                    'source': 'Page 2, item 5'}])
    for ref, rate, amount, included, source, note in KENSAL_REV_B:
        if ref in lines:
            QuoteMapLine.objects.create(draft=draft, line=lines[ref], rate=Decimal(rate) if rate else None,
                                        amount=Decimal(amount) if amount else None, included=included,
                                        source=source, note=note)
    for text, source in (('Protection of finished joinery after handover to follow-on trades.', 'Page 2, notes'),
                         ('Deliveries by appointment, two weeks\' notice.', 'Page 2, notes')):
        QuoteMapExclusion.objects.create(draft=draft, text=text, source=source)
    AIUsage.objects.create(user=estimator, feature=QUOTE_MAPPING, project=kings, model='claude-opus-5-5',
                           status=AIUsage.OK, input_tokens=6100, output_tokens=3800, cost_gbp=Decimal('0.08'),
                           note='7 scope lines, PDF', created_at=now - timedelta(hours=3))
    return draft
