"""Tender analytics: how bids convert, by sector, client, size, source and estimator."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .models import Project

PERIODS = [('12', 'Last 12 months'), ('24', 'Last 24 months'), ('all', 'All time')]

VALUE_BANDS = [
    (Decimal('50000'), 'Under 50k'),
    (Decimal('150000'), '50k – 150k'),
    (Decimal('500000'), '150k – 500k'),
    (Decimal('1000000'), '500k – 1m'),
    (None, '1m and over'),
]


def value_band(v):
    if v is None:
        return 'No value'
    for limit, label in VALUE_BANDS:
        if limit is None or v < limit:
            return label


class Tally:
    __slots__ = ('label', 'won', 'lost', 'won_value', 'lost_value')

    def __init__(self, label):
        self.label = label
        self.won = self.lost = 0
        self.won_value = self.lost_value = Decimal('0')

    def add(self, p):
        if p.stage in Project.WON_STAGES:
            self.won += 1
            self.won_value += p.value or 0
        elif p.stage == Project.STAGE_LOST:
            self.lost += 1
            self.lost_value += p.value or 0

    @property
    def bids(self):
        return self.won + self.lost

    @property
    def win_rate(self):
        return round(100 * self.won / self.bids) if self.bids else None

    @property
    def value_win_rate(self):
        total = self.won_value + self.lost_value
        return round(100 * self.won_value / total) if total else None


def _breakdown(bids, key, limit=None, order=None):
    groups = {}
    for p in bids:
        label = key(p) or 'Not recorded'
        groups.setdefault(label, Tally(label)).add(p)
    rows = list(groups.values())
    if order:
        rows.sort(key=lambda t: order.index(t.label) if t.label in order else len(order))
    else:
        rows.sort(key=lambda t: (-t.bids, -t.won_value, t.label))
    return rows[:limit] if limit else rows


def _months_back(today, n):
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(date(y, m, 1))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def tender_analytics(projects, period='12', today=None):
    today = today or date.today()
    period = period if period in dict(PERIODS) else '12'
    decided = projects.filter(stage__in=Project.DECIDED_STAGES, decided_at__isnull=False).select_related('owner')
    if period != 'all':
        decided = decided.filter(decided_at__gte=today - timedelta(days=365 * int(period) // 12))
    decided = list(decided)
    bids = [p for p in decided if p.stage != Project.STAGE_DECLINED]
    declined = len(decided) - len(bids)

    total = Tally('All')
    for p in bids:
        total.add(p)
    days_to_decision = [(p.decided_at - p.created_at.date()).days for p in bids
                        if p.created_at.date() <= p.decided_at]

    # Outcomes per month, newest on the right
    n_months = 12 if period == '12' else 24
    months = _months_back(today, n_months)
    per_month = defaultdict(lambda: [0, 0])
    for p in bids:
        k = date(p.decided_at.year, p.decided_at.month, 1)
        per_month[k][0 if p.stage in Project.WON_STAGES else 1] += 1
    peak = max([sum(per_month[m]) for m in months] + [1])
    monthly = []
    for m in months:
        won, lost = per_month[m]
        monthly.append({'month': m, 'won': won, 'lost': lost,
                        'won_h': f'{won / peak * 100:.2f}', 'lost_h': f'{lost / peak * 100:.2f}'})

    open_bids = projects.filter(stage__in=Project.PIPELINE_STAGES)
    stages = []
    for code in Project.PIPELINE_STAGES:
        items = [p for p in open_bids if p.stage == code]
        stages.append({'label': dict(Project.STAGE_CHOICES)[code], 'count': len(items),
                       'value': sum((p.value or 0) for p in items),
                       'weighted': sum((p.weighted_value or 0) for p in items)})

    sector_labels = dict(Project.SECTOR_CHOICES)
    band_order = [label for _, label in VALUE_BANDS] + ['No value']
    discipline = bid_discipline(decided, bids)
    return {
        **discipline,
        'period': period,
        'total': total,
        'declined': declined,
        'tendered_value': total.won_value + total.lost_value,
        'avg_won_value': total.won_value / total.won if total.won else None,
        'avg_days_to_decision': round(sum(days_to_decision) / len(days_to_decision)) if days_to_decision else None,
        'monthly': monthly,
        'monthly_has_data': any(m['won'] or m['lost'] for m in monthly),
        'stages': stages,
        'pipeline_value': sum(s['value'] for s in stages),
        'pipeline_weighted': sum(s['weighted'] for s in stages),
        'by_sector': _breakdown(bids, lambda p: sector_labels.get(p.sector)),
        'by_band': _breakdown(bids, lambda p: value_band(p.value), order=band_order),
        'by_client': _breakdown(bids, lambda p: p.client_name.strip(), limit=10),
        'by_source': _breakdown(bids, lambda p: p.lead_source.strip()),
        'by_owner': _breakdown(bids, lambda p: p.owner.full_name if p.owner else ''),
        'recent_losses': sorted((p for p in bids if p.stage == Project.STAGE_LOST),
                                key=lambda p: p.decided_at, reverse=True)[:8],
    }


NOT_SCORED = 'Not scored'
NOT_RECORDED = 'Not recorded'


class Share:
    __slots__ = ('label', 'count', 'pct')

    def __init__(self, label, count, total):
        self.label, self.count = label, count
        self.pct = round(100 * count / total) if total else 0


def _counts(labels, order):
    total = len(labels)
    tally = {}
    for label in labels:
        tally[label] = tally.get(label, 0) + 1
    rows = [Share(label, tally[label], total) for label in order if label in tally]
    return sorted(rows, key=lambda c: (-c.count, order.index(c.label)))


def bid_discipline(decided, bids):
    """
    From the bid/no-bid and loss reviews (tenders.BidReview): win rate by score band, why we
    decline, why we lose and by how much. Each project's review is attached as `p.review`.
    """
    from tenders.models import BidReview
    from tenders.scoring import DECLINE_REASONS, LOSS_REASONS, RESULT_CHOICES
    reviews = {r.project_id: r for r in BidReview.objects.filter(project_id__in=[p.pk for p in decided])}
    for p in decided:
        p.review = reviews.get(p.pk)
    result_labels = dict(RESULT_CHOICES)
    score_order = [label for _, label in RESULT_CHOICES] + [NOT_SCORED]
    by_score = _breakdown(bids, lambda p: result_labels.get(p.review.result) if p.review and p.review.result
                          else NOT_SCORED, order=score_order)
    declined = [p for p in decided if p.stage == Project.STAGE_DECLINED]
    decline_labels = dict(DECLINE_REASONS)
    decline_reasons = _counts([decline_labels.get(p.review.decline_reason, NOT_RECORDED) if p.review else NOT_RECORDED
                               for p in declined], [label for _, label in DECLINE_REASONS] + [NOT_RECORDED])
    lost = sorted((p for p in bids if p.stage == Project.STAGE_LOST), key=lambda p: p.decided_at, reverse=True)
    loss_labels = dict(LOSS_REASONS)
    loss_reasons = _counts([loss_labels.get(p.review.loss_reason, NOT_RECORDED) if p.review else NOT_RECORDED
                            for p in lost], [label for _, label in LOSS_REASONS] + [NOT_RECORDED])
    gaps = [p.review.gap_pct for p in lost if p.review and p.review.gap_pct is not None]
    return {
        'by_score': by_score,
        'scored_bids': sum(r.bids for r in by_score if r.label != NOT_SCORED),
        'decline_reasons': decline_reasons,
        'loss_reasons': loss_reasons,
        'loss_reviews': lost[:10],
        'losses_reviewed': sum(1 for p in lost if p.review and p.review.loss_reason),
        'losses_total': len(lost),
        'avg_price_gap': (sum(gaps) / len(gaps)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if gaps else None,
        'price_gaps_known': len(gaps),
    }
