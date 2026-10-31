"""
"Not levelled" on the trade coverage board (tenders.coverage). A trade counts as levelled once
at least MIN_LEVELLED subcontractors have priced lines on its levelling sheet.

    from levelling.coverage import annotate
    annotate([(project, rows), ...])   # sets row.levelled, row.not_levelled, row.levelling_url

Internal only, like the board itself.
"""
from django.urls import reverse

from .services import levelled_counts

MIN_LEVELLED = 2


def annotate(tenders):
    tenders = list(tenders)
    counts = levelled_counts(p.pk for p, _ in tenders)
    for project, rows in tenders:
        for r in rows:
            trade_id = r.trade.pk if r.trade else None
            r.levelled = counts.get((project.pk, trade_id), 0)
            r.not_levelled = r.trade is not None and not r.appointed and r.levelled < MIN_LEVELLED
            r.levelling_url = reverse('levelling_sheet', args=[project.organization.slug, project.pk, trade_id]) \
                if trade_id else ''
    return tenders
