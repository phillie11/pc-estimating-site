"""
Take "(From SV-002, Lund Joinery Ltd)" out of client variation descriptions. The sub variation
decision used to add it when it drafted a client variation, so the client saw the subcontractor's
name and the SV ref once the variation was submitted. It moves to the internal source_note.

Only text Siteline added is touched: the description must end with "\n\n(From SV-nnn, ...)" and
that SV ref must be a sub variation recovered as this variation (SubVariation.client_variation).
Anything a person wrote stays as it is.

Reversible: going back puts the source_note back on the end of the description, as it was, and
clears it. Historical models only.
"""
import re

from django.db import migrations

SUFFIX = re.compile(r'\n\n\(From (SV-\d{3,}), [^\n]*\)\s*$')


def forwards(apps, schema_editor):
    Variation = apps.get_model('variations', 'Variation')
    SubVariation = apps.get_model('delivery', 'SubVariation')
    refs = {}
    for sv in SubVariation.objects.filter(client_variation__isnull=False).order_by('pk'):
        refs.setdefault(sv.client_variation_id, set()).add(f'SV-{sv.number:03d}')
    for vo in Variation.objects.filter(pk__in=list(refs)).order_by('pk'):
        found = SUFFIX.search(vo.description or '')
        if not found or found.group(1) not in refs[vo.pk]:
            continue
        vo.source_note = found.group(0).strip()[1:-1][:300]  # "From SV-002, Lund Joinery Ltd"
        vo.description = vo.description[:found.start()]
        vo.save(update_fields=['description', 'source_note'])


def backwards(apps, schema_editor):
    Variation = apps.get_model('variations', 'Variation')
    for vo in Variation.objects.exclude(source_note='').filter(source_note__startswith='From SV-').order_by('pk'):
        vo.description = f'{vo.description}\n\n({vo.source_note})'
        vo.source_note = ''
        vo.save(update_fields=['description', 'source_note'])


class Migration(migrations.Migration):

    dependencies = [
        ("delivery", "0003_subvariation_change_event"),
        ("variations", "0008_variation_source_note"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
