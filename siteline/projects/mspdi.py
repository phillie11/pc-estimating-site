"""
Read a Microsoft Project XML file (MSPDI: File > Save As > XML in Microsoft Project; Asta
Powerproject, Primavera and most planning tools export the same format) into plain task dicts.

    tasks, warnings = read(uploaded_file)       # raises NotMspdi with a plain-English reason

Pure: no database, no Django models. The import service (projects/programme_import.py) compares
the result with the project's programme and applies it.

Safety:
  * parsed with defusedxml, with DTDs forbidden outright (MSPDI never has one), so entity
    expansion ("billion laughs") and external entities are refused before anything is read
  * at most MAX_BYTES read from the upload before any parsing
  * streamed (defusedxml's iterparse), each row dropped once read, so no large tree is built;
    more than MAX_TASKS <Task> rows is refused as soon as the row after the cap is reached
  * numbers that don't fit (task IDs over MAX_UID, "1e999") and dates outside MS Project's own
    range (EARLIEST to LATEST) are left out with a warning, never stored; warnings are capped
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
import io
from datetime import date, datetime, timedelta
from xml.etree.ElementTree import ParseError  # the exception class only: parsing is defusedxml's

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import iterparse

MAX_BYTES = 5 * 1024 * 1024
MAX_TASKS = 3000
MAX_DEPTH = 20  # MSPDI nests about 5 deep
MAX_UID = 2 ** 31 - 1  # what Activity.source_uid can hold on every database
EARLIEST, LATEST = date(1984, 1, 1), date(2149, 12, 31)  # the dates MS Project itself allows
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
        except (TypeError, ValueError, OverflowError):  # 'abc', 'nan', '1e999'
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


class _Warnings:
    """Warnings for the preview, a few of each kind: a hostile file can't make thousands of them."""
    SHOWN = 3

    def __init__(self):
        self.named, self.counts = {}, {}

    def add(self, kind, text=None):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if text is not None and len(self.named.setdefault(kind, [])) < self.SHOWN:
            self.named[kind].append(text)

    def count(self, kind):
        return self.counts.get(kind, 0)

    def lines(self, summaries):
        """summaries: [(kind, fn(n) -> line)]; named lines first, then 'and n more' when some were cut."""
        out = []
        for kind, summary in summaries:
            n = self.counts.get(kind, 0)
            if not n:
                continue
            named = self.named.get(kind, [])
            if named and n <= self.SHOWN:
                out.extend(named)
            else:
                out.append(summary(n))
        return out


def _plural(n, one, many):
    return one if n == 1 else many


def _in_range(d):
    return d is not None and EARLIEST <= d.date() <= LATEST


def parse(data):
    """
    Stream the XML (defusedxml's iterparse: no DTDs, entities or external references) and keep only
    what's needed, dropping each row once read, so a hostile file can't build a large tree in memory.
    The task cap is applied to the rows as they are read, before the rest of the file is parsed.
    """
    stack, raw_tasks, seen_root, has_tasks = [], 0, False, False
    rows, names, on_task = [], {}, {}
    try:
        for event, el in iterparse(io.BytesIO(data), events=('start', 'end'), forbid_dtd=True,
                                   forbid_entities=True, forbid_external=True):
            if event == 'start':
                if not seen_root:
                    seen_root = True
                    if _local(el.tag) != 'Project' or _ns(el.tag) not in (NS, ''):
                        raise NotMspdi(f'That XML file isn\'t a Microsoft Project programme. {HOW_TO_SAVE}')
                stack.append(el)
                if len(stack) > MAX_DEPTH:
                    raise NotMspdi(f'That XML file isn\'t a Microsoft Project programme. {HOW_TO_SAVE}')
                if len(stack) == 2 and _local(el.tag) == 'Tasks':
                    has_tasks = True
                continue
            stack.pop()
            if len(stack) != 2:  # only whole rows (Project > Tasks/Resources/Assignments > row) are read
                if len(stack) == 1:
                    stack[0].remove(el)  # a finished section: nothing left in it to keep
                continue
            section, row = _local(stack[1].tag), _local(el.tag)
            if section == 'Tasks' and row == 'Task':
                raw_tasks += 1
                if raw_tasks > MAX_TASKS + 1:  # + the project's own summary row (UID 0)
                    raise NotMspdi(f'The programme has more than {MAX_TASKS} tasks. Split it, or save a summary '
                                   'version for the site, and try again.')
                rows.append(_task_row(el))
            elif section == 'Resources' and row == 'Resource':
                uid, rname = _int(_text(el, 'UID')), _text(el, 'Name')
                if uid is not None and uid > 0 and rname:
                    names[uid] = rname[:WHO_MAX]
            elif section == 'Assignments' and row == 'Assignment':
                tuid, ruid = _int(_text(el, 'TaskUID')), _int(_text(el, 'ResourceUID'))
                if tuid is not None and ruid is not None:
                    on_task.setdefault(tuid, {})[ruid] = None  # an ordered set: file order
            stack[1].remove(el)  # drop the row once read
    except DefusedXmlException:
        raise NotMspdi('The file uses XML features we don\'t accept for safety (a DTD or entities). '
                       f'Save it again straight from your planning software. {HOW_TO_SAVE}')
    except (ParseError, ValueError) as e:
        if isinstance(e, NotMspdi):
            raise
        raise NotMspdi('The file isn\'t complete, valid XML: it may have been cut short or edited by hand. '
                       f'Save it again and upload the new copy. {HOW_TO_SAVE}')
    if not has_tasks:
        raise NotMspdi('The file is a Microsoft Project file but has no tasks in it. Check you saved the right '
                       'project.')
    return _tasks(rows, names, on_task)


def _task_row(t):
    """The fields of one <Task> element, as text, before any rules are applied."""
    return {
        'uid': _text(t, 'UID'), 'id': _text(t, 'ID'), 'level': _text(t, 'OutlineLevel'), 'is_null': _flag(t, 'IsNull'),
        'active': _text(t, 'Active'), 'name': _text(t, 'Name'), 'start': _text(t, 'Start'),
        'finish': _text(t, 'Finish'), 'milestone': _flag(t, 'Milestone'), 'summary': _flag(t, 'Summary'),
        'wbs': _text(t, 'WBS') or _text(t, 'OutlineNumber'), 'progress': _text(t, 'PercentComplete'),
        'preds': [_text(p, 'PredecessorUID') for p in _children(t, 'PredecessorLink')][:20],
    }


def _tasks(rows, names, on_task):
    tasks, warn = [], _Warnings()
    seen = set()
    for t in rows:
        uid = _int(t['uid'])
        level = _int(t['level'], 1)
        if uid is None or uid < 0:
            continue
        if uid == 0 or level == 0 or t['is_null']:
            continue  # the project's own summary row, and blank rows
        if uid > MAX_UID:
            warn.add('bad_uid')
            continue
        if t['active'] == '0':
            warn.add('inactive')
            continue
        if uid in seen:
            warn.add('duplicate', f'Task ID {uid} appears twice; the second copy was left out.')
            continue
        name = ' '.join(t['name'].split())[:NAME_MAX] or f'Task {t["id"][:12] or uid}'
        start, finish = parse_datetime(t['start']), parse_datetime(t['finish'])
        if start is None:
            warn.add('undated')
            continue
        if not _in_range(start):
            warn.add('out_of_range', f'"{name}" starts on a date MS Project can\'t use; it was left out.')
            continue
        if finish is not None and not _in_range(finish):
            warn.add('bad_finish', f'"{name}" has a finish date MS Project can\'t use; its finish was set to its start.')
            finish = None
        milestone = t['milestone']
        end = start.date() if milestone or finish is None else finish_date(start, finish)
        if end < start.date():
            warn.add('backwards', f'"{name}" finishes before it starts; its finish was set to its start.')
            end = start.date()
        preds = [_int(p) for p in t['preds']]
        progress = max(0, min(100, _int(t['progress'], 0) or 0))
        who = []
        for ruid in on_task.get(uid, ()):
            if ruid in names and names[ruid] not in who:
                who.append(names[ruid])
        seen.add(uid)
        tasks.append({
            'uid': uid,
            'name': name,
            'start': start.date().isoformat(),
            'finish': end.isoformat(),
            'milestone': milestone,
            'summary': t['summary'],
            'level': max(1, min(level or 1, 20)),
            'wbs': t['wbs'][:WBS_MAX],
            'progress': progress,
            'after_uid': next((p for p in preds if p), None),
            'who': ', '.join(who)[:WHO_MAX],
        })
        if len(tasks) > MAX_TASKS:
            raise NotMspdi(f'The programme has more than {MAX_TASKS} tasks. Split it, or save a summary '
                           'version for the site, and try again.')
    warnings = warn.lines([
        ('duplicate', lambda n: f'{n} task{_plural(n, "", "s")} repeated a task ID already in the file; '
                                f'{_plural(n, "it was", "they were")} left out.'),
        ('backwards', lambda n: f'{n} task{_plural(n, "", "s")} finished before {_plural(n, "it", "they")} started; '
                                'the finish was set to the start.'),
        ('out_of_range', lambda n: f'{n} task{_plural(n, "", "s")} with a start date outside 1984 to 2149 '
                                   f'{_plural(n, "was", "were")} left out.'),
        ('bad_finish', lambda n: f'{n} task{_plural(n, "", "s")} had a finish date outside 1984 to 2149; '
                                 'the finish was set to the start.'),
        ('bad_uid', lambda n: f'{n} task{_plural(n, "", "s")} with an unusable task ID {_plural(n, "was", "were")} '
                              'left out.'),
        ('undated', lambda n: f'{n} task{_plural(n, "", "s")} with no start date {_plural(n, "was", "were")} left out.'),
        ('inactive', lambda n: f'{n} inactive task{_plural(n, "", "s")} {_plural(n, "was", "were")} left out.'),
    ])
    if not tasks:
        raise NotMspdi('No dated tasks were found in the file. Check the programme has tasks with start dates.')
    known = {t['uid'] for t in tasks}
    for t in tasks:
        if t['after_uid'] not in known:
            t['after_uid'] = None
    return tasks, warnings


def as_date(value):
    return value if isinstance(value, date) else date.fromisoformat(value)
