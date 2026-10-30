"""
Bid/no-bid scoring: six weighted questions, a score out of 100 and Go / Review / No-go.

Pure functions and data only, so the rules sit in one place with a fixed test table
(tenders/test_bid.py, ScoringTableTests). Weights, answers and band limits are data:
change them here and the table tells you what moved.

The score is advice for the person deciding, not a decision. Advisory: check your
contract and your own judgement before committing to a bid.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

GO, REVIEW, NO_GO = 'GO', 'REVIEW', 'NOGO'
RESULT_CHOICES = [(GO, 'Go'), (REVIEW, 'Review'), (NO_GO, 'No-go')]
RESULT_TONES = {GO: 'good', REVIEW: 'warn', NO_GO: 'bad'}
GO_FROM = 65       # score at or above: Go
REVIEW_FROM = 45   # at or above (and below GO_FROM): Review; below: No-go
MAX_POINTS = 4     # every answer is worth 0 to 4 points


@dataclass(frozen=True)
class Question:
    key: str
    label: str
    weight: int
    help: str
    options: tuple  # (points, label), best first


QUESTIONS = (
    Question('client', 'Client relationship', 20, 'Do we know them, and do they pay?', (
        (4, 'Repeat client, we work well together'),
        (3, 'Known to us, or a strong referral'),
        (2, 'New client, nothing against them'),
        (1, 'Difficult relationship in the past'),
        (0, 'Poor payment record or disputes'),
    )),
    Question('sector', 'Sector fit', 15, 'Have we done this kind of job recently?', (
        (4, 'Core sector, similar jobs recently'),
        (2, 'Some experience in this sector'),
        (0, 'Outside our experience'),
    )),
    Question('value', 'Value band', 15, 'Is the size right for us?', (
        (4, 'In our usual range'),
        (2, 'Bigger or smaller than we usually do'),
        (0, 'Well outside what we can carry'),
    )),
    Question('programme', 'Programme realism', 15, 'Can it be built in the time allowed?', (
        (4, 'Realistic, with some float'),
        (2, 'Tight but achievable'),
        (0, 'Unrealistic'),
    )),
    Question('workload', 'Team workload', 15, "Other open bids the estimator is pricing over the same weeks.", (
        (4, 'Clear: 0 or 1 other bid at the same time'),
        (3, '2 other bids at the same time'),
        (2, '3 other bids at the same time'),
        (1, '4 other bids at the same time'),
        (0, '5 or more other bids at the same time'),
    )),
    Question('terms', 'Contract terms and competition', 20, 'The form of contract, its amendments and how many are bidding.', (
        (4, 'Standard form, 3 bidders or fewer'),
        (2, 'Some amendments, or 4 to 6 bidders'),
        (0, 'Onerous terms, or 7 bidders or more'),
    )),
)
QUESTION_KEYS = tuple(q.key for q in QUESTIONS)
assert sum(q.weight for q in QUESTIONS) == 100


def score(answers):
    """
    answers: {question key: points 0..4}. Returns the score out of 100 (whole number, half up),
    or None unless every question is answered.
    """
    if any(answers.get(k) is None for k in QUESTION_KEYS):
        return None
    total = Decimal(0)
    for q in QUESTIONS:
        points = int(answers[q.key])
        if not 0 <= points <= MAX_POINTS:
            raise ValueError(f'{q.key}: {points} is not between 0 and {MAX_POINTS}')
        total += Decimal(q.weight * points) / MAX_POINTS
    return int(total.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def result(points):
    if points is None:
        return ''
    if points >= GO_FROM:
        return GO
    if points >= REVIEW_FROM:
        return REVIEW
    return NO_GO


def workload_points(other_bids):
    """Overlapping open bids for the same estimator, to the workload answer."""
    return {0: 4, 1: 4, 2: 3, 3: 2, 4: 1}.get(other_bids, 0)


def terms_points(contract_form, amendments, bidders):
    """A suggestion from the intake fields, or None when there's too little to go on."""
    if contract_form == 'BESPOKE' or (bidders is not None and bidders >= 7):
        return 0
    if (amendments or '').strip() or (bidders is not None and bidders >= 4):
        return 2
    if contract_form and bidders is not None:
        return 4
    return None


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

CONTRACT_FORMS = [
    ('JCT_MW', 'JCT Minor Works (MW / MWD)'),
    ('JCT_IC', 'JCT Intermediate (IC / ICD)'),
    ('JCT_DB', 'JCT Design and Build (DB)'),
    ('JCT_SBC', 'JCT Standard Building Contract (SBC)'),
    ('NEC4_A', 'NEC4 ECC Option A: priced, activity schedule'),
    ('NEC4_B', 'NEC4 ECC Option B: priced, bill of quantities'),
    ('NEC4_C', 'NEC4 ECC Option C: target, activity schedule'),
    ('NEC4_D', 'NEC4 ECC Option D: target, bill of quantities'),
    ('NEC4_E', 'NEC4 ECC Option E: cost reimbursable'),
    ('NEC4_F', 'NEC4 ECC Option F: management contract'),
    ('BESPOKE', "Bespoke or client's own terms"),
]

DECLINE_OTHER = 'OTHER'
DECLINE_REASONS = [
    ('BUSY', 'Too busy'),
    ('SECTOR', 'Wrong sector'),
    ('TERMS', 'Onerous terms'),
    ('BIDDERS', 'Too many bidders'),
    ('PAYMENT', 'Poor payment history'),
    ('PROGRAMME', 'Programme unrealistic'),
    (DECLINE_OTHER, 'Other'),
]

LOSS_OTHER = 'OTHER'
LOSS_REASONS = [
    ('PRICE', 'Price'),
    ('PROGRAMME', 'Programme'),
    ('QUALITY', 'Quality or technical score'),
    ('SCOPE', 'Qualifications or exclusions'),
    ('RELATIONSHIP', 'Client went with someone they know'),
    ('NOT_PROCEEDING', 'Client did not go ahead'),
    ('NO_FEEDBACK', 'No feedback given'),
    (LOSS_OTHER, 'Other'),
]


def price_gap(ours, winner):
    """How far above (+) or below (-) the winning price we were, as a % to 1 decimal place."""
    if ours is None or not winner:
        return None
    return ((Decimal(ours) - Decimal(winner)) / Decimal(winner) * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
