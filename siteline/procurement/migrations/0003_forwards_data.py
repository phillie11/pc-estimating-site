# Data: move each revision's single forward (forwarded_to, forward_note, consultant_comments...)
# into a SubmittalForward row, and link that revision's designer mark-ups to it. Historical
# models only. Reverse copies the latest forward per revision back into the columns, unlinks
# the files and deletes the rows (earlier forwards of the same revision can't be kept there).
from django.db import migrations


def forwards(apps, schema_editor):
    Revision = apps.get_model('procurement', 'SubmittalRevision')
    Forward = apps.get_model('procurement', 'SubmittalForward')
    File = apps.get_model('procurement', 'SubmittalFile')
    for rev in Revision.objects.exclude(forwarded_to__isnull=True).exclude(forwarded_at__isnull=True):
        fwd = Forward.objects.create(
            organization_id=rev.organization_id, revision_id=rev.pk, consultant_id=rev.forwarded_to_id,
            forwarded_by_id=rev.forwarded_by_id, forwarded_at=rev.forwarded_at, note=rev.forward_note,
            comments=rev.consultant_comments, replied_at=rev.consultant_replied_at)
        File.objects.filter(revision_id=rev.pk, role='CONSULTANT').update(forward=fwd)


def backwards(apps, schema_editor):
    Revision = apps.get_model('procurement', 'SubmittalRevision')
    Forward = apps.get_model('procurement', 'SubmittalForward')
    File = apps.get_model('procurement', 'SubmittalFile')
    for fwd in Forward.objects.order_by('revision_id', 'forwarded_at', 'pk'):
        # Later forwards of the same revision overwrite earlier ones: the old columns hold one
        Revision.objects.filter(pk=fwd.revision_id).update(
            forwarded_to_id=fwd.consultant_id, forwarded_by_id=fwd.forwarded_by_id, forwarded_at=fwd.forwarded_at,
            forward_note=fwd.note, consultant_comments=fwd.comments, consultant_replied_at=fwd.replied_at)
    File.objects.exclude(forward__isnull=True).update(forward=None)
    Forward.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('procurement', '0002_submittalforward'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
