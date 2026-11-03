"""
Bring an MS Project XML programme into a project's programme: compare, preview, then apply.

    imp = stage(project, uploaded_file, user)        # reads it (projects/mspdi.py); NotMspdi if it can't
    plan = compare(imp)                              # what applying would do, for the preview page
    result = apply(imp, user, remove_missing=False)  # does it, in one transaction

Rules (also in the README):
  * Each task is matched to a programme line by its MS Project unique ID (UID), so importing an
    updated file changes those lines and never duplicates them. Lines added by hand in Siteline
    have no UID and are never touched by an import.
  * From the file: name, start, finish, milestone, summary (heading) line, WBS, outline level,
    % complete and the first predecessor ("Follows").
  * Kept from Siteline: whether a line is shared with collaborators, and its subcontractor and
    trade once set. Resource names in the file fill "Who"; a resource named exactly like a
    subcontractor appointed on the job (or a company trade) sets the subcontractor (or trade) on
    a line that has none.
  * Lines from an earlier import that aren't in this file are kept unless the person ticks
    "remove them" on the preview.
  * A preview can't be applied once a file uploaded after it has been applied (Overtaken): it would
    put the older dates back. Applies on one project run one at a time (the project row is locked).
"""
from django.db import transaction
from django.utils import timezone

from . import mspdi
from .models import Activity, ProgrammeImport, Project



class Overtaken(ValueError):
    """A newer file has been applied since this one was uploaded. str() is shown to the user."""


# Fields a re-import may change, as (field on Activity, key in the task dict)
FROM_FILE = (('name', 'name'), ('start', 'start'), ('finish', 'finish'), ('is_milestone', 'milestone'),
             ('is_summary', 'summary'), ('outline_level', 'level'), ('wbs', 'wbs'), ('progress', 'progress'))
LABELS = {'name': 'name', 'start': 'start', 'finish': 'finish', 'is_milestone': 'milestone', 'is_summary': 'heading',
          'outline_level': 'outline', 'wbs': 'WBS', 'progress': '% done', 'after': 'follows', 'responsible': 'who'}


def stage(project, f, user):
    tasks, warnings = mspdi.read(f)
    name = (getattr(f, 'name', '') or 'programme.xml').rsplit('/', 1)[-1][-200:]
    return ProgrammeImport.objects.create(project=project, file_name=name, tasks=tasks, warnings=warnings,
                                          uploaded_by=user)


def _value(task, key):
    v = task[key]
    return mspdi.as_date(v) if key in ('start', 'finish') else v


def _matchers(project):
    """Resource name (lower case) -> appointed subcontractor, and -> trade."""
    from directory.models import Package, Trade
    subs, trades = {}, {}
    for p in Package.objects.filter(project=project, status=Package.APPOINTED).select_related('subcontractor', 'trade'):
        subs.setdefault(p.subcontractor.company_name.strip().lower(), (p.subcontractor, p.trade))
    for t in Trade.objects.all():
        trades[t.name.strip().lower()] = t
    return subs, trades


def compare(imp):
    """
    {'new': [task], 'changed': [(activity, task, [labels])], 'same': n, 'missing': [activity], 'by_uid': {...}}
    for the preview. Reads the programme as it is now, so a preview opened later is still right.
    """
    existing = {a.source_uid: a for a in Activity.objects.filter(project=imp.project, source_uid__isnull=False)
                .select_related('after')}
    uid_of = {a.pk: a.source_uid for a in existing.values()}
    new, changed, same = [], [], 0
    in_file = set()
    for task in imp.tasks:
        in_file.add(task['uid'])
        a = existing.get(task['uid'])
        shown = {**task, 'start': _value(task, 'start'), 'finish': _value(task, 'finish')}  # dates, for the page
        if a is None:
            new.append(shown)
            continue
        diffs = [LABELS[field] for field, key in FROM_FILE if getattr(a, field) != _value(task, key)]
        if uid_of.get(a.after_id) != task['after_uid']:
            diffs.append(LABELS['after'])
        if task['who'] and a.responsible != task['who']:
            diffs.append(LABELS['responsible'])
        if diffs:
            changed.append((a, shown, diffs))
        else:
            same += 1
    missing = [a for uid, a in existing.items() if uid not in in_file]
    missing.sort(key=lambda a: (a.start, a.name))
    return {'new': new, 'changed': changed, 'same': same, 'missing': missing}


@transaction.atomic
def apply(imp, user, remove_missing=False):
    """Create and update lines from the staged import. Returns {'created', 'updated', 'removed'}."""
    # One apply at a time per project: two at once would both create the same task IDs
    project = Project.objects.select_for_update().get(pk=imp.project_id)
    imp = ProgrammeImport.objects.select_for_update().get(pk=imp.pk, project=project)
    if imp.status != ProgrammeImport.PREVIEW:
        raise ValueError('This import has already been applied or discarded.')
    if ProgrammeImport.objects.filter(project=project, status=ProgrammeImport.APPLIED, pk__gt=imp.pk).exists():
        raise Overtaken(f'A programme file uploaded after {imp.file_name} has already been applied, so applying '
                        'this one would put the older dates back. If this file really is the latest, upload it '
                        'again.')
    existing = {a.source_uid: a for a in Activity.objects.filter(project=project, source_uid__isnull=False)}
    subs, trades = _matchers(project)
    created = 0
    touched = set()  # existing lines this import changed
    by_uid = {}
    for task in imp.tasks:
        a = existing.get(task['uid'])
        is_new = a is None
        if is_new:
            a = Activity(project=project, source_uid=task['uid'], created_by=user)
        before = None if is_new else [getattr(a, f) for f, _ in FROM_FILE] + [a.responsible]
        for field, key in FROM_FILE:
            setattr(a, field, _value(task, key))
        if task['who']:
            a.responsible = task['who']
            match = subs.get(task['who'].strip().lower())
            if match and a.subcontractor_id is None:
                a.subcontractor = match[0]
                if a.trade_id is None and match[1] is not None:
                    a.trade = match[1]
            elif a.trade_id is None and task['who'].strip().lower() in trades:
                a.trade = trades[task['who'].strip().lower()]
        if is_new or before != [getattr(a, f) for f, _ in FROM_FILE] + [a.responsible]:
            a.save()
            if is_new:
                created += 1
            else:
                touched.add(a.pk)
        by_uid[task['uid']] = a
    # Predecessors once every line exists ("Follows" can point at a task further down the file)
    for task in imp.tasks:
        a = by_uid[task['uid']]
        after = by_uid.get(task['after_uid'])
        after_id = after.pk if after is not None and after.pk != a.pk else None
        if a.after_id != after_id:
            a.after_id = after_id
            a.save(update_fields=['after'])
            if task['uid'] in existing:
                touched.add(a.pk)
    updated = len(touched)
    removed = 0
    if remove_missing:
        stale = Activity.objects.filter(project=project, source_uid__isnull=False) \
            .exclude(source_uid__in=list(by_uid))
        removed = stale.count()
        stale.delete()
    imp.status, imp.applied_by, imp.applied_at = ProgrammeImport.APPLIED, user, timezone.now()
    imp.result = {'created': created, 'updated': updated, 'removed': removed}
    imp.save(update_fields=['status', 'applied_by', 'applied_at', 'result'])
    return imp.result


def discard(imp):
    if imp.status == ProgrammeImport.PREVIEW:
        imp.status = ProgrammeImport.DISCARDED
        imp.save(update_fields=['status'])
