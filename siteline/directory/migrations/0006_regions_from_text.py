"""
Regions become a list (data step). Each subcontractor's free-text regions ("North West,
Yorkshire", "National") are read best-effort into region codes. The text itself is left
exactly as typed, now labelled "Region notes", so nothing is lost if the guess is wrong.

Reverse empties the lists again; the text was never changed. Uses the historical model.
guess_regions() is a pure function over strings (directory/regions.py), not a model, so
importing it doesn't tie this migration to today's models.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from directory.regions import guess_regions
    Subcontractor = apps.get_model('directory', 'Subcontractor')
    for s in Subcontractor.objects.exclude(regions='').only('pk', 'regions', 'region_codes').iterator():
        codes = guess_regions(s.regions)
        if codes:
            Subcontractor.objects.filter(pk=s.pk).update(region_codes=codes)


def backwards(apps, schema_editor):
    Subcontractor = apps.get_model('directory', 'Subcontractor')
    Subcontractor.objects.update(region_codes=[])


class Migration(migrations.Migration):

    dependencies = [
        ('directory', '0005_regions_and_chasing'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
