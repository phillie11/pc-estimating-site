"""
Respond-by dates for variations already waiting on the client: due_date = submitted date + 14 days
(the default; a contract's own client_response_days only applies to submissions from now on). The
record index mirrors due_date, so its rows are updated too. Only rows with no due date are touched.

Reverse clears exactly what this set: submitted variations whose due date is still submitted + 14.
Historical models only.
"""
from datetime import timedelta

from django.db import migrations
from django.utils import timezone

DAYS = 14


def _submitted(apps):
    return apps.get_model('variations', 'Variation').objects.filter(status='SUBMITTED', submitted_at__isnull=False)


def _index(apps, vo):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ct = ContentType.objects.filter(app_label='variations', model='variation').first()
    return apps.get_model('core', 'RecordIndex').objects.filter(content_type=ct, object_id=vo.pk) if ct else None


def _respond_by(vo):
    return timezone.localdate(vo.submitted_at) + timedelta(days=DAYS)


def forwards(apps, schema_editor):
    for vo in _submitted(apps).filter(due_date__isnull=True).iterator():
        vo.due_date = _respond_by(vo)
        vo.save(update_fields=['due_date'])
        rows = _index(apps, vo)
        if rows is not None:
            rows.update(due_date=vo.due_date)


def backwards(apps, schema_editor):
    for vo in _submitted(apps).filter(due_date__isnull=False).iterator():
        if vo.due_date == _respond_by(vo):
            vo.due_date = None
            vo.save(update_fields=['due_date'])
            rows = _index(apps, vo)
            if rows is not None:
                rows.update(due_date=None)


class Migration(migrations.Migration):

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("core", "0002_audit_actor_name_retention"),
        ("variations", "0006_decision_evidence"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
