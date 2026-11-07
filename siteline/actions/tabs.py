"""
How a project's tabs are laid out. projects.views.project_context decides which tabs a
person may see (`tabs`: code, label, url, count); this decides how they are shown.

Staff on a won or live job: eight grouped tabs (STAFF_GROUPS), no "More" menu. A group with more
than one register shows them as a second row of section links under the tab, so everything is at
most two taps away. A bid keeps its short flat row (BID_ORDER). Any tab no group claims goes after
the groups; only if that ever takes the row past MAX_STAFF_TABS does a "More" menu appear.

External collaborators: at most five tabs (Overview, Decisions, Documents, Programme,
Snags). Registers that live under one of them (RFIs, variations, RAMS, tasks, the team)
show as a second row of links under that tab.
"""
from django.urls import reverse

MAX_STAFF_TABS = 8

# The order tabs earn a place in the row; anything not listed goes after these
# 'commercial' (contract, change register, client variations) replaced the staff 'variations' tab
STAFF_ORDER = ('overview', 'programme', 'documents', 'rfis', 'commercial', 'variations', 'subvariations', 'rams', 'procurement', 'diary', 'issues',
               'tender', 'packages', 'tasks', 'team', 'quality',
               'closeout', 'safety', 'meetings')
# Staff groups on a won or live job: (code, label, member tab codes). The group's link is its first
# member the person can see; its count is the members' counts added up.
STAFF_GROUPS = (
    ('overview', 'Overview', ('overview', 'team')),
    ('programme', 'Programme', ('programme', 'meetings', 'tasks')),
    ('documents', 'Documents', ('documents',)),
    ('rfis', 'RFIs', ('rfis',)),
    ('site', 'Site', ('diary', 'issues', 'quality', 'safety', 'rams')),
    ('commercial', 'Commercial', ('commercial', 'subvariations')),
    ('supply', 'Supply chain', ('packages', 'tender', 'procurement')),
    ('closeout', 'Close-out', ('closeout',)),
)
# How a member is labelled in its group's section links, where its tab label would repeat the group
STAFF_GROUP_MEMBER_LABELS = {
    'overview': 'Summary',
    'programme': 'Programme',
    'commercial': 'Contract and money',
}
# Estimating keeps its own order: the tender comes first on a bid
BID_ORDER = ('overview', 'tender', 'packages', 'documents', 'team')

STAFF_LABELS = {
    'variations': 'Variations to client',
    'subvariations': 'Variations from subs',
    'issues': 'Snags',
}

# External tab groups, in row order
EXTERNAL_GROUPS = (
    ('overview', 'Overview', ('overview', 'team')),
    ('decisions', 'Decisions', ('decisions', 'rfis', 'variations', 'payments', 'subpayments', 'subvariations', 'rams', 'submittals', 'tasks', 'meetings')),
    ('documents', 'Documents', ('documents',)),
    ('programme', 'Programme', ('programme',)),
    ('snags', 'Snags', ('issues', 'closeout')),
)
EXTERNAL_LABELS = {
    'decisions': 'Waiting on you',
    'team': "Who's who",
    'rfis': 'RFIs',
    'variations': 'Variations',
    'payments': 'Applications for payment',
    'subpayments': 'Your payments',
    'subvariations': 'Your variations',
    'rams': 'Your RAMS',
    'submittals': 'Submittals',
    'tasks': 'Your tasks',
    'meetings': 'Meetings',
    'issues': 'Your snags',
    'closeout': 'Defects and handover',
}


def _ordered(tabs, order):
    rank = {code: i for i, code in enumerate(order)}
    return sorted(tabs, key=lambda t: rank.get(t[0], len(rank)))


def arrange(request, project, tabs, active):
    """Context for project_base.html: tab_row, tab_more, more_on, subtabs, tab_group and, for externals, changes."""
    from projects.access import is_internal
    if is_internal(request):
        return _staff(tabs, active, project)
    return _external(request, project, tabs, active)


def _staff(tabs, active, project):
    tabs = [(code, STAFF_LABELS.get(code, label), url, n) for code, label, url, n in tabs]
    if project.section != 'estimating':
        return _staff_grouped(tabs, active)
    tabs = _ordered(tabs, BID_ORDER)
    row, more = tabs, []
    if len(tabs) > MAX_STAFF_TABS:
        row, more = tabs[:MAX_STAFF_TABS - 1], tabs[MAX_STAFF_TABS - 1:]
    on = next((t for t in more if t[0] == active), None)
    return {'tabs': tabs, 'tab_row': row, 'tab_more': more, 'more_on': on,
            'more_count': sum(n or 0 for _, _, _, n in more), 'tab_group': active, 'subtabs': []}


def _external(request, project, tabs, active):
    import actions

    from . import visits
    by_code = {t[0]: t for t in tabs}
    if 'tender' in by_code and len(tabs) == 1:  # a tendering subcontractor: their tender is all there is
        return {'tabs': tabs, 'tab_row': tabs, 'tab_more': [], 'more_on': None, 'more_count': 0,
                'tab_group': active, 'subtabs': []}
    waiting = actions.for_user(request, project=project)
    by_code['decisions'] = ('decisions', 'Waiting on you',
                            reverse('project_decisions', args=[request.org.slug, project.pk]), len(waiting))
    row, subtabs, group_on = [], [], active
    for group, label, members in EXTERNAL_GROUPS:
        present = [by_code[c] for c in members if c in by_code]
        if group == 'decisions' and len(present) == 1:
            present = []  # nothing to decide on and no registers: leave the tab out
        if not present:
            continue
        first = present[0]
        count = len(waiting) if group == 'decisions' else (first[3] if group == 'snags' else None)
        row.append((group, label, first[2], count))
        if active in members:
            group_on = group
            if len(present) > 1:
                subtabs = [(c, EXTERNAL_LABELS.get(c, lbl), url, n) for c, lbl, url, n in present]
    since = visits.touch(request, project)
    ctx = {'tabs': tabs, 'tab_row': row, 'tab_more': [], 'more_on': None, 'more_count': 0,
           'tab_group': group_on, 'subtabs': subtabs, 'last_visit': since}
    if active == 'overview':
        ctx['changes'] = visits.changes_since(request, project, since)
    return ctx


def _staff_grouped(tabs, active):
    by_code = {t[0]: t for t in tabs}
    claimed = {c for _, _, members in STAFF_GROUPS for c in members}
    row, subtabs, group_on = [], [], active
    for group, label, members in STAFF_GROUPS:
        present = [by_code[c] for c in members if c in by_code]
        if not present:
            continue
        count = sum(t[3] or 0 for t in present) or None
        row.append((group, label, present[0][2], count))
        if active in members:
            group_on = group
            if len(present) > 1:
                subtabs = [(c, STAFF_GROUP_MEMBER_LABELS.get(c, lbl), url, n) for c, lbl, url, n in present]
    row += _ordered([t for t in tabs if t[0] not in claimed], STAFF_ORDER)
    more = []
    if len(row) > MAX_STAFF_TABS:
        row, more = row[:MAX_STAFF_TABS - 1], row[MAX_STAFF_TABS - 1:]
    on = next((t for t in more if t[0] == active), None)
    return {'tabs': tabs, 'tab_row': row, 'tab_more': more, 'more_on': on,
            'more_count': sum(n or 0 for _, _, _, n in more), 'tab_group': group_on, 'subtabs': subtabs,
            'subtabs_grouped': True}
