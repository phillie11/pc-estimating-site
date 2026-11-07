"""
Fill Penetration.sealed_by for seals recorded before it was stored (data only; 0002 added the column),
so the "someone else signs off fire-stopping" rule also covers them: whoever made the latest seal or
reseal transition in the record's audit trail. Uses the historical models only.

Reverse does nothing: 0002's reverse drops the column.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Penetration = apps.get_model('quality', 'Penetration')
    RecordIndex = apps.get_model('core', 'RecordIndex')
    AuditEntry = apps.get_model('core', 'AuditEntry')
    rows = Penetration.objects.filter(sealed_by__isnull=True, status__in=('SEALED', 'FAILED', 'SIGNED_OFF'))
    for pen in rows.order_by('pk').iterator():
        idx = RecordIndex.objects.filter(record_type='quality.penetration', object_id=pen.pk).values_list(
            'pk', flat=True).first()
        if idx is None:
            continue
        actor = AuditEntry.objects.filter(record_idx_id=idx, action__in=('seal', 'reseal'), actor__isnull=False) \
            .order_by('-at', '-pk').values_list('actor_id', flat=True).first()
        if actor is not None:
            Penetration.objects.filter(pk=pen.pk).update(sealed_by_id=actor)


class Migration(migrations.Migration):
    dependencies = [
        ('quality', '0002_independent_fs_sign_off'),
        ('core', '0002_audit_actor_name_retention'),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
