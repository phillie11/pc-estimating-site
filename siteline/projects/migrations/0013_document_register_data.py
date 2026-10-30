"""
Fill the new register fields from what is already there.

Documents: read `reference` as an ISO 19650 number (project-originator-volume-level-type-
role-number) where it has the seven parts, and fill the fields from it. A number that
follows the UK National Annex lengths and default type and role codes is marked as
following the naming; anything else (including numbers that don't split into seven parts,
such as "1203-A-100") is marked "needs checking" with a note, so someone can fix or accept
it from the register. Blank numbers stay blank.

Revisions: the current revision is shared if its document was shared (published if it is a
construction issue, C01 and up), or work in progress if the document was internal. Every
other revision is superseded by its document's current one. So everything externals could
see before, they still see.

The parser below is a frozen copy of documents.naming, so later changes there can't alter
this migration. Reversing it empties the new fields again.
"""
import re

from django.db import migrations

FIELDS = ('project_code', 'originator', 'volume', 'level', 'doc_type', 'role', 'number')
LENGTHS = {'project_code': (2, 6), 'originator': (3, 6), 'volume': (1, 2), 'level': (2, 2),
           'doc_type': (2, 2), 'role': (1, 2), 'number': (4, 6)}
TYPES = {'AF', 'BQ', 'CA', 'CM', 'CO', 'CP', 'CR', 'DB', 'DR', 'FN', 'HS', 'IE', 'M2', 'M3', 'MI', 'MR', 'MS',
         'PP', 'PR', 'RD', 'RI', 'RP', 'SA', 'SC', 'SH', 'SK', 'SN', 'SP', 'SU', 'VS'}
ROLES = {'A': 'Architect', 'B': 'Building surveyor', 'C': 'Civil engineer', 'D': 'Drainage and highways engineer',
         'E': 'Electrical engineer', 'F': 'Facilities manager', 'G': 'Land surveyor',
         'H': 'Heating and ventilation designer', 'I': 'Interior designer', 'K': 'Client', 'L': 'Landscape architect',
         'M': 'Mechanical engineer', 'P': 'Public health engineer', 'Q': 'Quantity surveyor', 'S': 'Structural engineer',
         'T': 'Town planner', 'W': 'Contractor', 'X': 'Subcontractor', 'Y': 'Specialist designer',
         'Z': 'General (more than one discipline)'}
LABELS = {'project_code': 'Project', 'originator': 'Originator', 'volume': 'Volume / system',
          'level': 'Level / location', 'doc_type': 'Type', 'role': 'Role', 'number': 'Number'}
PART_RE = re.compile(r'^[A-Z0-9]{1,12}$')
CONSTRUCTION_RE = re.compile(r'^\s*C\s*-?\s*\d+\s*$', re.IGNORECASE)


def split(text):
    parts = [re.sub(r'\s+', '', p).upper() for p in (text or '').strip().split('-')]
    if len(parts) != 7 or not all(PART_RE.match(p) for p in parts):
        return None
    return dict(zip(FIELDS, parts))


def warnings_for(parts):
    out = []
    for f in FIELDS:
        v = parts[f]
        lo, hi = LENGTHS[f]
        if f == 'number' and not v.isdigit():
            out.append(f'Number "{v}" should be digits only.')
        elif not lo <= len(v) <= hi:
            out.append(f'{LABELS[f]} "{v}" should be {lo if lo == hi else f"{lo} to {hi}"} characters.')
    if parts['doc_type'] not in TYPES:
        out.append(f'Type "{parts["doc_type"]}" isn\'t in this project\'s code list.')
    if parts['role'] not in ROLES:
        out.append(f'Role "{parts["role"]}" isn\'t in this project\'s code list.')
    return out


def fill(apps, schema_editor):
    Document = apps.get_model('projects', 'Document')
    DocumentRevision = apps.get_model('projects', 'DocumentRevision')
    for doc in Document._base_manager.iterator():
        ref = (doc.reference or '').strip()
        if not ref:
            continue
        parts = split(ref)
        if parts is None:
            doc.naming = 'UNPARSED'
            doc.naming_notes = f'"{ref}" couldn\'t be read as an ISO 19650 number. Fix it, or accept it as it is.'
        else:
            for f in FIELDS:
                setattr(doc, f, parts[f])
            doc.reference = '-'.join(parts[f] for f in FIELDS)
            found = warnings_for(parts)
            doc.naming = 'UNPARSED' if found else 'OK'
            doc.naming_notes = ' '.join(found)[:500]
            if not doc.discipline:
                doc.discipline = ROLES.get(parts['role'], '')
        doc.save(update_fields=['reference', 'naming', 'naming_notes', 'discipline', *FIELDS])

    shared = set(Document._base_manager.filter(visibility='SHARED').values_list('pk', flat=True))
    by_doc = {}
    for r in DocumentRevision._base_manager.only('pk', 'document_id', 'revision', 'is_current', 'uploaded_at').iterator():
        by_doc.setdefault(r.document_id, []).append(r)
    for doc_id, revisions in by_doc.items():
        current = next((r for r in revisions if r.is_current), None) or max(revisions, key=lambda r: (r.uploaded_at, r.pk))
        for r in revisions:
            if r.pk == current.pk:
                if doc_id in shared:
                    status = 'PUBLISHED' if CONSTRUCTION_RE.match(r.revision or '') else 'SHARED'
                else:
                    status = 'WIP'
                DocumentRevision._base_manager.filter(pk=r.pk).update(status=status, status_changed_at=r.uploaded_at)
            else:
                DocumentRevision._base_manager.filter(pk=r.pk).update(status='SUPERSEDED', superseded_by=current,
                                                                       status_changed_at=r.uploaded_at)


def empty(apps, schema_editor):
    Document = apps.get_model('projects', 'Document')
    DocumentRevision = apps.get_model('projects', 'DocumentRevision')
    Document._base_manager.update(naming='', naming_notes='', discipline='', **{f: '' for f in FIELDS})
    DocumentRevision._base_manager.update(status='', status_changed_at=None, superseded_by=None)


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0012_document_register_fields'),
    ]

    operations = [
        migrations.RunPython(fill, empty),
    ]
