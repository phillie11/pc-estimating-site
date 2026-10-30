"""
UK regions a subcontractor covers, and a best-effort reading of free text.

The regions are the twelve UK nations and regions (England's nine plus Wales,
Scotland and Northern Ireland) and "Nationwide". `guess_regions()` turns text
such as "North West, Yorkshire" or "48 Call Lane, Leeds LS1" into region codes.
It is a keyword match and gets things wrong at the edges (a sub who wrote
"the North" is put down for all three northern regions), so the UI always says
matching is approximate.
"""
import re

NATIONWIDE = 'UK'

REGION_CHOICES = [
    ('NE', 'North East'),
    ('NW', 'North West'),
    ('YH', 'Yorkshire and the Humber'),
    ('EM', 'East Midlands'),
    ('WM', 'West Midlands'),
    ('EE', 'East of England'),
    ('LDN', 'London'),
    ('SE', 'South East'),
    ('SW', 'South West'),
    ('WAL', 'Wales'),
    ('SCO', 'Scotland'),
    ('NI', 'Northern Ireland'),
    (NATIONWIDE, 'Nationwide'),
]
REGION_LABELS = dict(REGION_CHOICES)

# Phrases (lower case, matched on word boundaries) and the regions they mean.
# Longer phrases are tried first so "north west" wins over "north".
KEYWORDS = {
    'nationwide': [NATIONWIDE], 'national': [NATIONWIDE], 'uk wide': [NATIONWIDE], 'uk-wide': [NATIONWIDE],
    'all uk': [NATIONWIDE], 'whole uk': [NATIONWIDE], 'all areas': [NATIONWIDE], 'anywhere': [NATIONWIDE],
    'england and wales': ['NE', 'NW', 'YH', 'EM', 'WM', 'EE', 'LDN', 'SE', 'SW', 'WAL'],
    'north east': ['NE'], 'north-east': ['NE'], 'northeast': ['NE'], 'newcastle': ['NE'], 'sunderland': ['NE'],
    'durham': ['NE'], 'teesside': ['NE'], 'middlesbrough': ['NE'], 'northumberland': ['NE'], 'gateshead': ['NE'],
    'north west': ['NW'], 'north-west': ['NW'], 'northwest': ['NW'], 'manchester': ['NW'], 'liverpool': ['NW'],
    'merseyside': ['NW'], 'lancashire': ['NW'], 'cheshire': ['NW'], 'cumbria': ['NW'], 'preston': ['NW'],
    'salford': ['NW'], 'trafford': ['NW'], 'altrincham': ['NW'], 'stockport': ['NW'], 'bolton': ['NW'],
    'wigan': ['NW'], 'blackburn': ['NW'], 'blackpool': ['NW'], 'chester': ['NW'], 'warrington': ['NW'],
    'ellesmere port': ['NW'], 'wirral': ['NW'], 'spinningfields': ['NW'], 'carlisle': ['NW'],
    'yorkshire': ['YH'], 'humber': ['YH'], 'leeds': ['YH'], 'sheffield': ['YH'], 'bradford': ['YH'],
    'york': ['YH'], 'hull': ['YH'], 'harrogate': ['YH'], 'wakefield': ['YH'], 'huddersfield': ['YH'],
    'doncaster': ['YH'],
    'midlands': ['EM', 'WM'],
    'east midlands': ['EM'], 'nottingham': ['EM'], 'derby': ['EM'], 'leicester': ['EM'], 'lincoln': ['EM'],
    'northampton': ['EM'],
    'west midlands': ['WM'], 'birmingham': ['WM'], 'coventry': ['WM'], 'wolverhampton': ['WM'],
    'worcester': ['WM'], 'shrewsbury': ['WM'], 'stoke': ['WM'], 'pershore': ['WM'], 'hereford': ['WM'],
    'east anglia': ['EE'], 'east of england': ['EE'], 'norwich': ['EE'], 'cambridge': ['EE'], 'ipswich': ['EE'],
    'essex': ['EE'], 'norfolk': ['EE'], 'suffolk': ['EE'], 'hertfordshire': ['EE'], 'peterborough': ['EE'],
    'london': ['LDN'], 'greater london': ['LDN'], 'm25': ['LDN', 'SE', 'EE'], 'home counties': ['SE', 'EE'],
    'south east': ['SE'], 'south-east': ['SE'], 'southeast': ['SE'], 'kent': ['SE'], 'surrey': ['SE'],
    'sussex': ['SE'], 'brighton': ['SE'], 'oxford': ['SE'], 'reading': ['SE'], 'southampton': ['SE'],
    'portsmouth': ['SE'], 'milton keynes': ['SE'], 'hampshire': ['SE'], 'berkshire': ['SE'],
    'south west': ['SW'], 'south-west': ['SW'], 'southwest': ['SW'], 'bristol': ['SW'], 'bath': ['SW'],
    'exeter': ['SW'], 'plymouth': ['SW'], 'cornwall': ['SW'], 'devon': ['SW'], 'somerset': ['SW'],
    'gloucester': ['SW'], 'cheltenham': ['SW'], 'dorset': ['SW'], 'bournemouth': ['SW'], 'swindon': ['SW'],
    'wales': ['WAL'], 'cardiff': ['WAL'], 'swansea': ['WAL'], 'newport': ['WAL'], 'wrexham': ['WAL'],
    'scotland': ['SCO'], 'glasgow': ['SCO'], 'edinburgh': ['SCO'], 'aberdeen': ['SCO'], 'dundee': ['SCO'],
    'inverness': ['SCO'],
    'northern ireland': ['NI'], 'belfast': ['NI'], 'derry': ['NI'],
    'the north': ['NE', 'NW', 'YH'], 'north of england': ['NE', 'NW', 'YH'], 'the south': ['SE', 'SW', 'LDN'],
}

# London postcode districts (EC, WC, E, N, NW, SE, SW, W) at the end of an address, e.g. "Kingsway, London WC2"
_LONDON_POSTCODE = re.compile(r'\b(EC|WC|E|N|NW|SE|SW|W)\d{1,2}[A-Z]?\b')
_PATTERNS = [(re.compile(r'(?<![a-z])' + re.escape(k) + r'(?![a-z])'), codes)
             for k, codes in sorted(KEYWORDS.items(), key=lambda kv: -len(kv[0]))]


def guess_regions(text):
    """Region codes a piece of free text seems to mean, in REGION_CHOICES order. Best effort."""
    if not text:
        return []
    low = ' ' + text.lower() + ' '
    found = set()
    for pattern, codes in _PATTERNS:
        if pattern.search(low):
            found.update(codes)
            low = pattern.sub(' ', low)  # "north west" must not also count as "north"
    if _LONDON_POSTCODE.search(text) and 'london' in text.lower():
        found.add('LDN')
    order = [c for c, _ in REGION_CHOICES]
    return [c for c in order if c in found]


def labels(codes):
    return [REGION_LABELS[c] for c in codes if c in REGION_LABELS]


def covers(sub_codes, wanted):
    """
    How well a subcontractor's regions fit the regions a job is in:
    'yes' (covers it, or nationwide), 'unknown' (either side has no regions), 'no'.
    """
    if NATIONWIDE in (sub_codes or []):
        return 'yes'
    if not sub_codes or not wanted:
        return 'unknown'
    return 'yes' if set(sub_codes) & set(wanted) else 'no'
