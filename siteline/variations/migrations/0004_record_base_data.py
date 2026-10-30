"""
Client variations onto the record base, step 2 of 3: data. Every existing variation keeps its
id and number and gets:

  - ref ("VO-003"),
  - visibility: drafts INTERNAL, everything else SHARED (what clients could already see),
  - ball in court: the project's first client while it is submitted (waiting on their decision),
  - a NumberSequence per project carrying on after the highest number used,
  - its RecordIndex row and a back-filled 'created' AuditEntry (who raised it and when),
  - a RecordLink "came from" its RFI where Variation.rfi is set (the FK stays too, for now).

Reversible: going back removes the variations' sequence, index, link and audit rows; 0003's
reverse then renames created_by back to raised_by and drops the new columns. Uses the historical
models only; reverse() is used for the index row's link, left blank if the URL has gone.
"""
from django.db import migrations
from django.urls import NoReverseMatch, reverse

KEY = 'variations.variation'
NOTE = 'Back-filled when variations moved onto the record base (variations migration 0004).'
LINK_NOTE = 'Back-filled from Variation.rfi (variations migration 0004).'


def forwards(apps, schema_editor):
    Variation = apps.get_model('variations', 'Variation')
    if not Variation.objects.exists():
        return  # a new database: nothing to carry over
    ContentType = apps.get_model('contenttypes', 'ContentType')
    NumberSequence = apps.get_model('core', 'NumberSequence')
    RecordIndex = apps.get_model('core', 'RecordIndex')
    RecordLink = apps.get_model('core', 'RecordLink')
    AuditEntry = apps.get_model('core', 'AuditEntry')
    ProjectMember = apps.get_model('projects', 'ProjectMember')
    ct, _ = ContentType.objects.get_or_create(app_label='variations', model='variation')
    rfi_ct = ContentType.objects.filter(app_label='rfis', model='rfi').first()
    labels = dict(Variation._meta.get_field('status').choices)

    first_client = {}
    for m in ProjectMember.objects.filter(role='CLIENT').order_by('pk'):
        first_client.setdefault(m.project_id, m.user_id)

    highest = {}
    for vo in Variation.objects.select_related('organization').order_by('pk').iterator():
        vo.ref = f'VO-{vo.number:03d}'
        vo.visibility = 'INTERNAL' if vo.status == 'DRAFT' else 'SHARED'
        vo.ball_in_court_user_id = first_client.get(vo.project_id) if vo.status == 'SUBMITTED' else None
        vo.save(update_fields=['ref', 'visibility', 'ball_in_court_user'])
        key = (vo.organization_id, vo.project_id)
        highest[key] = max(highest.get(key, 0), vo.number)
        try:
            url = reverse('variation_detail', args=[vo.organization.slug, vo.project_id, vo.pk])
        except NoReverseMatch:
            url = ''
        idx = RecordIndex.objects.create(
            organization_id=vo.organization_id, content_type=ct, object_id=vo.pk, record_type=KEY,
            project_id=vo.project_id, ref=vo.ref, title=vo.title[:200], status=vo.status,
            status_label=str(labels.get(vo.status, vo.status))[:60], is_open=vo.status == 'SUBMITTED',
            ball_in_court_user_id=vo.ball_in_court_user_id, due_date=vo.due_date, visibility=vo.visibility,
            url=url, created_at=vo.created_at, updated_at=vo.updated_at)
        AuditEntry.objects.create(organization_id=vo.organization_id, record_idx=idx, actor_id=vo.created_by_id,
                                  action='created', changes={}, note=NOTE, at=vo.created_at)
        if vo.rfi_id and rfi_ct is not None:
            rfi_idx = RecordIndex.objects.filter(content_type=rfi_ct, object_id=vo.rfi_id).first()
            if rfi_idx is not None and rfi_idx.organization_id == vo.organization_id:
                RecordLink.objects.create(organization_id=vo.organization_id, from_idx=idx, to_idx=rfi_idx,
                                          kind='derived_from', created_by_id=vo.created_by_id)
                for a, b in ((idx, rfi_idx), (rfi_idx, idx)):
                    AuditEntry.objects.create(organization_id=vo.organization_id, record_idx=a, actor_id=None,
                                              action='linked', changes={'link': [None, f'derived_from {b.ref}']},
                                              note=LINK_NOTE, at=vo.created_at)
    NumberSequence.objects.bulk_create([
        NumberSequence(organization_id=org_id, project_id=project_id, key=KEY, next_value=n + 1)
        for (org_id, project_id), n in highest.items()
    ])


def backwards(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ct = ContentType.objects.filter(app_label='variations', model='variation').first()
    if ct is not None:
        RecordIndex = apps.get_model('core', 'RecordIndex')
        RecordLink = apps.get_model('core', 'RecordLink')
        AuditEntry = apps.get_model('core', 'AuditEntry')
        ids = list(RecordIndex.objects.filter(content_type=ct).values_list('pk', flat=True))
        # The one place audit rows are removed: undoing the migration that made them. That includes
        # the 'linked'/'unlinked' entries on the other end (an RFI's) that name a variation.
        others = set(RecordLink.objects.filter(to_idx__in=ids).values_list('from_idx', flat=True)) \
            | set(RecordLink.objects.filter(from_idx__in=ids).values_list('to_idx', flat=True))
        stale = [e.pk for e in AuditEntry.objects.filter(record_idx__in=others, action__in=('linked', 'unlinked'))
                 if any(' VO-' in str(v) for v in (e.changes or {}).get('link', []))]
        AuditEntry.objects.filter(pk__in=stale).delete()
        AuditEntry.objects.filter(record_idx__in=ids).delete()
        RecordLink.objects.filter(from_idx__in=ids).delete()
        RecordLink.objects.filter(to_idx__in=ids).delete()
        RecordIndex.objects.filter(content_type=ct).delete()
    apps.get_model('core', 'NumberSequence').objects.filter(key=KEY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("core", "0002_audit_actor_name_retention"),
        ("projects", "0011_project_archive"),
        ("variations", "0003_record_base_fields"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
