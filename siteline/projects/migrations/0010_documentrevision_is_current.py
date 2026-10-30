import re

from django.db import migrations, models

# A frozen copy of projects.models.revision_rank / latest_revision, so later changes there can't alter this migration
SERIES = {'P': 0, 'C': 1}
REVISION_RE = re.compile(r'^\s*([PC])\s*-?\s*(\d+)\s*$', re.IGNORECASE)


def rank(code):
    m = REVISION_RE.match(code or '')
    return (SERIES[m.group(1).upper()], int(m.group(2))) if m else None


def flag_current(apps, schema_editor):
    """For every document, flag the highest P/C revision (P before C, numerically), or the latest upload if any code is odd."""
    DocumentRevision = apps.get_model('projects', 'DocumentRevision')
    by_doc = {}
    for r in DocumentRevision.objects.only('pk', 'document_id', 'revision', 'uploaded_at').iterator():
        by_doc.setdefault(r.document_id, []).append(r)
    current = []
    for revisions in by_doc.values():
        if all(rank(r.revision) for r in revisions):
            best = max(revisions, key=lambda r: (rank(r.revision), r.uploaded_at, r.pk))
        else:
            best = max(revisions, key=lambda r: (r.uploaded_at, r.pk))
        current.append(best.pk)
    for i in range(0, len(current), 500):
        DocumentRevision.objects.filter(pk__in=current[i:i + 500]).update(is_current=True)


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0009_file_scan'),
    ]

    operations = [
        migrations.AddField(
            model_name='documentrevision',
            name='is_current',
            field=models.BooleanField(default=False, help_text='Set by Document.add_revision / set_current; one per document.',
                                      verbose_name='Current revision'),
        ),
        # Reversing drops the column, so the backwards data step has nothing to undo
        migrations.RunPython(flag_current, migrations.RunPython.noop),
    ]
