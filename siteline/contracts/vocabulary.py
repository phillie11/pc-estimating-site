"""
The words each form of contract uses, in one place.

NEC talks about compensation events, early warnings and the Project Manager; JCT about
variations, instructions, Relevant Events and the Contract Administrator (the Employer's
Agent under Design and Build). Screens ask vocabulary(form) for the right word instead of
hard-coding one, so a JCT job never shows "compensation event" and an NEC job never shows
"Relevant Event".

    v = vocabulary('NEC4_ECC')
    v['change']         -> 'compensation event'
    v['administrator']  -> 'Project Manager'
"""

FAMILY_JCT = 'JCT'
FAMILY_NEC = 'NEC'
FAMILY_OTHER = 'OTHER'

_JCT = {
    'family': FAMILY_JCT,
    'change': 'variation',
    'change_plural': 'variations',
    'instruction': "Contract Administrator's instruction",
    'delay_event': 'Relevant Event',
    'delay_event_plural': 'Relevant Events',
    'administrator': 'Contract Administrator',
    'client_party': 'Employer',
    'early_warning': 'early warning',
    'early_warning_note': 'JCT has no early warning procedure. Use these as your own risk log, and give the '
                          'contractual notices the form asks for (notice of delay, for example).',
    'early_warning_meeting': 'risk review meeting',
    'notice_of_delay': 'notice of delay',
    'time_extension': 'extension of time',
    'completion': 'Practical Completion',
    'completion_date': 'Date for Completion',
    'possession_date': 'Date of Possession',
    'damages': 'liquidated damages',
    'defects_period': 'Rectification Period',
    'valuation_date': 'Interim Valuation Date',
    'payment_document': 'Interim Certificate',
    'quotation': 'variation quotation',
}

_NEC = {
    'family': FAMILY_NEC,
    'change': 'compensation event',
    'change_plural': 'compensation events',
    'instruction': "Project Manager's instruction",
    'delay_event': 'compensation event',
    'delay_event_plural': 'compensation events',
    'administrator': 'Project Manager',
    'client_party': 'Client',
    'early_warning': 'early warning',
    'early_warning_note': 'Give an early warning as soon as you become aware of anything that could increase the '
                          'Prices, delay Completion or a Key Date, or impair the works (NEC4 clause 15).',
    'early_warning_meeting': 'early warning meeting',
    'notice_of_delay': '',  # NEC has no separate notice of delay: notify a compensation event
    'time_extension': 'change to the Completion Date',
    'completion': 'Completion',
    'completion_date': 'Completion Date',
    'possession_date': 'starting date',
    'damages': 'delay damages',
    'defects_period': 'defects date',
    'valuation_date': 'assessment date',
    'payment_document': 'payment certificate',
    'quotation': 'quotation',
}

_OTHER = {
    **_JCT,
    'family': FAMILY_OTHER,
    'instruction': 'instruction',
    'delay_event': 'delay event',
    'delay_event_plural': 'delay events',
    'administrator': 'contract administrator',
    'client_party': 'client',
    'early_warning_note': 'Use these as your own risk log, and give whatever notices your contract asks for.',
    'notice_of_delay': 'notice of delay',
    'completion': 'completion',
    'completion_date': 'completion date',
    'possession_date': 'start on site',
    'damages': 'liquidated damages',
    'defects_period': 'defects period',
    'valuation_date': 'valuation date',
    'payment_document': 'payment certificate',
    'quotation': 'quotation',
}


def family(form):
    """'JCT', 'NEC' or 'OTHER' for a MainContract.form value (None or '' counts as other)."""
    form = form or ''
    if form.startswith('JCT_'):
        return FAMILY_JCT
    if form.startswith('NEC'):
        return FAMILY_NEC
    return FAMILY_OTHER


def vocabulary(form):
    """The words for this form of contract. A new dict each call, so callers may change it."""
    words = {FAMILY_JCT: _JCT, FAMILY_NEC: _NEC}.get(family(form), _OTHER)
    words = dict(words)
    if form == 'JCT_DB':
        words['administrator'] = "Employer's Agent"
        words['instruction'] = "Employer's instruction"
        words['payment_document'] = 'Interim Payment'
    if form in ('JCT_MW', 'JCT_MWD'):
        words['possession_date'] = 'Date for Commencement'
        words['payment_document'] = 'interim certificate'
    return words


def vocabulary_for(project):
    """The words for a project's main contract; generic words when no contract is set up yet."""
    # A missing one-to-one raises RelatedObjectDoesNotExist, an AttributeError, so getattr's default applies
    contract = getattr(project, 'main_contract', None) if project is not None else None
    return vocabulary(contract.form if contract is not None else '')
