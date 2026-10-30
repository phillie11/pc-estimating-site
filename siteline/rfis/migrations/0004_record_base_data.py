"""
RFIs onto the record base, step 2 of 3: data. Every existing RFI keeps its number and gets:

  - ref ("RFI-007") and visibility (from is_shared),
  - a NumberSequence per project carrying on after the highest number used,
  - its RecordIndex row,
  - a back-filled 'created' AuditEntry (who raised it and when; no field values, which we
    don't know as they were then).

Reversible: going back copies visibility into is_shared and removes the RFIs' sequence, index,
link and audit rows. Uses the historical models only, so it doesn't depend on today's code;
the one exception is reverse() for the index row's link, which is left blank if the URL has gone.
"""
from django.db import migrations
from django.urls import NoReverseMatch, reverse

KEY = 'rfis.rfi'
NOTE = 'Back-filled when RFIs moved onto the record base (rfis migration 0004).'


def forwards(apps, schema_editor):
    RFI = apps.get_model('rfis', 'RFI')
    if not RFI.objects.exists():
        return  # a new database: nothing to carry over
    ContentType = apps.get_model('contenttypes', 'ContentType')
    NumberSequence = apps.get_model('core', 'NumberSequence')
    RecordIndex = apps.get_model('core', 'RecordIndex')
    AuditEntry = apps.get_model('core', 'AuditEntry')
    ct, _ = ContentType.objects.get_or_create(app_label='rfis', model='rfi')
    labels = dict(RFI._meta.get_field('status').choices)

    highest = {}
    for rfi in RFI.objects.select_related('organization').order_by('pk').iterator():
        rfi.ref = f'RFI-{rfi.number:03d}'
        rfi.visibility = 'SHARED' if rfi.is_shared else 'INTERNAL'
        rfi.save(update_fields=['ref', 'visibility'])
        key = (rfi.organization_id, rfi.project_id)
        highest[key] = max(highest.get(key, 0), rfi.number)
        try:
            url = reverse('rfi_detail', args=[rfi.organization.slug, rfi.project_id, rfi.pk])
        except NoReverseMatch:
            url = ''
        idx = RecordIndex.objects.create(
            organization_id=rfi.organization_id, content_type=ct, object_id=rfi.pk, record_type=KEY,
            project_id=rfi.project_id, ref=rfi.ref, title=rfi.title[:200], status=rfi.status,
            status_label=str(labels.get(rfi.status, rfi.status))[:60], is_open=rfi.status == 'OPEN',
            ball_in_court_user_id=rfi.ball_in_court_user_id, due_date=rfi.due_date, visibility=rfi.visibility,
            url=url, created_at=rfi.created_at, updated_at=rfi.updated_at)
        AuditEntry.objects.create(organization_id=rfi.organization_id, record_idx=idx, actor_id=rfi.created_by_id,
                                  action='created', changes={}, note=NOTE, at=rfi.created_at)
    NumberSequence.objects.bulk_create([
        NumberSequence(organization_id=org_id, project_id=project_id, key=KEY, next_value=n + 1)
        for (org_id, project_id), n in highest.items()
    ])


def backwards(apps, schema_editor):
    RFI = apps.get_model('rfis', 'RFI')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    for rfi in RFI.objects.order_by('pk').iterator():
        rfi.is_shared = rfi.visibility == 'SHARED'
        rfi.save(update_fields=['is_shared'])
    ct = ContentType.objects.filter(app_label='rfis', model='rfi').first()
    if ct is not None:
        # The one place audit rows are removed: undoing the migration that made them
        apps.get_model('core', 'AuditEntry').objects.filter(record_idx__content_type=ct).delete()
        RecordLink = apps.get_model('core', 'RecordLink')
        RecordLink.objects.filter(from_idx__content_type=ct).delete()
        RecordLink.objects.filter(to_idx__content_type=ct).delete()
        apps.get_model('core', 'RecordIndex').objects.filter(content_type=ct).delete()
    apps.get_model('core', 'NumberSequence').objects.filter(key=KEY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("core", "0001_records"),
        ("rfis", "0003_record_base_fields"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
