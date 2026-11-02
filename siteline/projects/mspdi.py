"""
Read a Microsoft Project XML file (MSPDI: File > Save As > XML in Microsoft Project; Asta
Powerproject, Primavera and most planning tools export the same format) into plain task dicts.

    tasks, warnings = read(uploaded_file)       # raises NotMspdi with a plain-English reason

Pure: no database, no Django models. The import service (projects/programme_import.py) compares
the result with the project's programme and applies it.

Safety:
  * parsed with defusedxml, with DTDs forbidden outright (MSPDI never has one), so entity
    expansion ("billion laughs") and external entities are refused before anything is read
  * at most MAX_BYTES read from the upload, and at most MAX_TASKS tasks kept
  * anything that isn't an MSPDI <Project> (a .mpp, an Asta .pp, an Excel sheet, any other XML)
    is refused with a message saying how to get the right file

What is read from each <Task>:
  UID           kept as the activity's source id, so a re-import updates rather than duplicates
  Name, Start, Finish (the date part), Milestone, Summary, OutlineLevel, WBS (or OutlineNumber),
  PercentComplete, the first PredecessorLink (its PredecessorUID) and assigned resource names.
Left out: the project summary task (UID 0 / OutlineLevel 0), blank rows (IsNull) and inactive
tasks (Active = 0). A finish at exactly midnight after the start means "end of the day before"
(some tools write it that way), so it is moved back a day.
"""
from datetime import date, datetime, timedelta
from xml.etree.ElementTree import ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

MAX_BYTES = 5 * 1024 * 1024
MAX_TASKS = 3000
NAME_MAX = 200
WHO_MAX = 120
WBS_MAX = 40
NS = 'http://schemas.microsoft.com/project'

HOW_TO_SAVE = ('In Microsoft Project use File > Save As and choose "XML Format (*.xml)". '
               'In Asta Powerproject use File > Save As and choose "Microsoft Project XML".')


class NotMspdi(ValueError):
    """The upload can't be read as a Microsoft Project XML programme. str() is shown to the user."""


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _ns(tag):
    return tag[1:].split('}', 1)[0] if tag.startswith('{') else ''


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _text(el, name, default=''):
    c = _child(el, name)
    return (c.text or '').strip() if c is not None and c.text else default


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _flag(el, name):
    return _text(el, name).lower() in ('1', 'true')


def parse_datetime(value):
    """'2026-11-02T08:00:00' (or just the date) -> datetime, else None."""
    value = (value or '').strip()
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value[:19] if 'T' in value else value[:10], fmt)
        except ValueError:
            continue
    return None


def finish_date(start, finish):
    """The last working day of a task: a finish at 00:00 after the start belongs to the day before."""
    if finish.time() == datetime.min.time() and finish > start:
        return (finish - timedelta(days=1)).date()
    return finish.date()


def sniff(head, name=''):
    """Refuse the common wrong files before parsing, with a reason that says what to do."""
    lower = (name or '').lower()
    if head.startswith(b'\xd0\xcf\x11\xe0') or lower.endswith('.mpp'):
        return f'That looks like a Microsoft Project .mpp file. Save it as XML first. {HOW_TO_SAVE}'
    if head.startswith(b'PK') or lower.endswith(('.xlsx', '.xls', '.csv', '.pp', '.pdf')):
        return f'That isn\'t a Microsoft Project XML file. {HOW_TO_SAVE}'
    if head.startswith(b'%PDF'):
        return f'That is a PDF. We need the programme as Microsoft Project XML. {HOW_TO_SAVE}'
    stripped = head.lstrip(b'\xef\xbb\xbf \t\r\n')
    if not (stripped.startswith(b'<') or head.startswith((b'\xff\xfe', b'\xfe\xff'))):
        return f'That isn\'t an XML file. {HOW_TO_SAVE}'
    return None


def read(f, name=None):
    """
    f: an uploaded file (or anything with read()); name: its file name if f has none.
    Returns (tasks, warnings). tasks is a list of dicts in file order.
    """
    data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise NotMspdi(f'The file is over {MAX_BYTES // (1024 * 1024)} MB. Save just this project\'s programme '
                       'as XML, without baselines you don\'t need, and try again.')
    if not data.strip():
        raise NotMspdi(f'The file is empty. {HOW_TO_SAVE}')
    problem = sniff(data[:8], name or getattr(f, 'name', ''))
    if problem:
        raise NotMspdi(problem)
    return parse(data)


def parse(data):
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException:
        raise NotMspdi('The file uses XML features we don\'t accept for safety (a DTD or entities). '
                       f'Save it again straight from your planning software. {HOW_TO_SAVE}')
    except (ParseError, ValueError):
        raise NotMspdi('The file isn\'t complete, valid XML: it may have been cut short or edited by hand. '
                       f'Save it again and upload the new copy. {HOW_TO_SAVE}')
    if _local(root.tag) != 'Project' or _ns(root.tag) not in (NS, ''):
        raise NotMspdi(f'That XML file isn\'t a Microsoft Project programme. {HOW_TO_SAVE}')
    tasks_el = _child(root, 'Tasks')
    if tasks_el is None:
        raise NotMspdi('The file is a Microsoft Project file but has no tasks in it. Check you saved the right '
                       'project.')

    # Resource names, and which resources are on which task
    names = {}
    resources = _child(root, 'Resources')
    for r in _children(resources, 'Resource') if resources is not None else ():
        uid, rname = _int(_text(r, 'UID')), _text(r, 'Name')
        if uid is not None and uid > 0 and rname:
            names[uid] = rname
    on_task = {}
    assignments = _child(root, 'Assignments')
    for a in _children(assignments, 'Assignment') if assignments is not None else ():
        tuid, ruid = _int(_text(a, 'TaskUID')), _int(_text(a, 'ResourceUID'))
        if tuid is not None and ruid in names and names[ruid] not in on_task.setdefault(tuid, []):
            on_task[tuid].append(names[ruid])

    tasks, warnings = [], []
    undated = inactive = 0
    seen = set()
    for t in _children(tasks_el, 'Task'):
        uid = _int(_text(t, 'UID'))
        level = _int(_text(t, 'OutlineLevel'), 1)
        if uid is None or uid < 0:
            continue
        if uid == 0 or level == 0 or _flag(t, 'IsNull'):
            continue  # the project's own summary row, and blank rows
        if _text(t, 'Active') == '0':
            inactive += 1
            continue
        if uid in seen:
            warnings.append(f'Task ID {uid} appears twice; the second copy was left out.')
            continue
        name = ' '.join(_text(t, 'Name').split())[:NAME_MAX] or f'Task {_text(t, "ID") or uid}'
        start, finish = parse_datetime(_text(t, 'Start')), parse_datetime(_text(t, 'Finish'))
        if start is None:
            undated += 1
            continue
        milestone = _flag(t, 'Milestone')
        end = start.date() if milestone or finish is None else finish_date(start, finish)
        if end < start.date():
            warnings.append(f'"{name}" finishes before it starts; its finish was set to its start.')
            end = start.date()
        preds = [_int(_text(p, 'PredecessorUID')) for p in _children(t, 'PredecessorLink')]
        progress = max(0, min(100, _int(_text(t, 'PercentComplete'), 0) or 0))
        seen.add(uid)
        tasks.append({
            'uid': uid,
            'name': name,
            'start': start.date().isoformat(),
            'finish': end.isoformat(),
            'milestone': milestone,
            'summary': _flag(t, 'Summary'),
            'level': max(1, min(level or 1, 20)),
            'wbs': (_text(t, 'WBS') or _text(t, 'OutlineNumber'))[:WBS_MAX],
            'progress': progress,
            'after_uid': next((p for p in preds if p), None),
            'who': ', '.join(on_task.get(uid, []))[:WHO_MAX],
        })
        if len(tasks) > MAX_TASKS:
            raise NotMspdi(f'The programme has more than {MAX_TASKS} tasks. Split it, or save a summary '
                           'version for the site, and try again.')
    if undated:
        warnings.append(f'{undated} task{"s" if undated != 1 else ""} with no start date '
                        f'{"were" if undated != 1 else "was"} left out.')
    if inactive:
        warnings.append(f'{inactive} inactive task{"s" if inactive != 1 else ""} '
                        f'{"were" if inactive != 1 else "was"} left out.')
    if not tasks:
        raise NotMspdi('No dated tasks were found in the file. Check the programme has tasks with start dates.')
    known = {t['uid'] for t in tasks}
    for t in tasks:
        if t['after_uid'] not in known:
            t['after_uid'] = None
    return tasks, warnings


def as_date(value):
    return value if isinstance(value, date) else date.fromisoformat(value)
