"""
The shared record base every register (RFIs first; variations, RAMS, documents to follow) builds on.
See docs/adr/0001-core-record-base.md for why it looks like this, and the README section
"Building a new record type" for how to adopt it.

  RecordBase      abstract TenantModel: project, number/ref, title, status, visibility, due date,
                  ball in court, created/updated by, soft delete, retention, legal hold, version.
  NumberSequence  the next number per (project, record type), locked while it is taken.
  RecordIndex     one row per record of any type, kept in step by RecordBase.save(): what
                  "Needs me", search and links read instead of querying every register.
  RecordLink      a typed link between two indexed records.
  AuditEntry      append-only history per record with field-level diffs.

Everything here is a TenantModel, so it is scoped to the current organisation and fails closed.
"""
import datetime
import decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, models, transaction
from django.db.models import Max
from django.db.models.fields.files import FieldFile
from django.utils import timezone

from tenancy.context import get_current_org
from tenancy.models import TenantManager, TenantModel, TenantQuerySet

from .exceptions import PurgeRefused, RecordConflict
from .registry import get_record_type

VIS_INTERNAL = 'INTERNAL'
VIS_SHARED = 'SHARED'
VISIBILITY_CHOICES = [(VIS_INTERNAL, 'Internal only'), (VIS_SHARED, 'Shared with project collaborators')]


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------

class NumberSequence(TenantModel):
    """
    The next number for one record type on one project (RFI-001, RFI-002...). next_number()
    locks the row with select_for_update, so two people raising an RFI at once can't get the
    same number, and only that project's RFI numbering waits, not the whole project row.
    """
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='+')
    key = models.CharField(max_length=60, help_text='Record type, e.g. "rfis.rfi".')
    next_value = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = [('project', 'key')]

    def __str__(self):
        return f'{self.key} #{self.next_value} on project {self.project_id}'


def next_number(record):
    """
    Take the next number for this record's type and project. Call inside the transaction that
    saves the record, so a failed save gives the number back. The first record of a type on a
    project creates the sequence, starting after any number already used (rows that predate it).
    """
    key = get_record_type(record).key
    # Pinned by organisation and project, so all_objects can't reach another company's sequence
    rows = NumberSequence.all_objects.select_for_update().filter(
        organization_id=record.organization_id, project_id=record.project_id, key=key)
    with transaction.atomic():
        seq = rows.first()
        if seq is None:
            used = type(record).all_objects.filter(project_id=record.project_id).aggregate(m=Max('number'))['m'] or 0
            try:
                with transaction.atomic():  # savepoint: someone else may create it first
                    seq = NumberSequence(organization_id=record.organization_id, project_id=record.project_id,
                                         key=key, next_value=used + 1)
                    seq.save()
            except IntegrityError:
                seq = rows.get()
        number = seq.next_value
        seq.next_value = number + 1
        seq.save(update_fields=['next_value'])
        return number


# ---------------------------------------------------------------------------
# The record base
# ---------------------------------------------------------------------------

class RecordManager(TenantManager):
    """`objects` on a record: scoped to the organisation like every TenantModel, and soft-deleted rows left out."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


def _json_value(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, FieldFile):
        return value.name or None
    return value


class RecordBase(TenantModel):
    """
    Subclass for every numbered, per-project register row. Then register it (core.registry)
    from the app's AppConfig.ready(). Save with record.save(actor=request.user) so the audit
    trail and updated_by know who did it; change status with core.workflow.apply_transition().

    Managers: `objects` hides soft-deleted rows, `with_deleted` doesn't (both scoped to the
    organisation), `all_objects` is the unscoped escape hatch as on every TenantModel.
    """
    VIS_INTERNAL = VIS_INTERNAL
    VIS_SHARED = VIS_SHARED
    VISIBILITY_CHOICES = VISIBILITY_CHOICES

    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='%(class)ss')
    number = models.PositiveIntegerField(editable=False)
    ref = models.CharField(max_length=24, editable=False)
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=20, db_index=True)
    visibility = models.CharField(max_length=8, choices=VISIBILITY_CHOICES, default=VIS_INTERNAL)
    due_date = models.DateField(null=True, blank=True)
    ball_in_court_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                           related_name='+', verbose_name='Ball in court')
    ball_in_court_company = models.ForeignKey('directory.Subcontractor', null=True, blank=True,
                                              on_delete=models.SET_NULL, related_name='+',
                                              verbose_name='Ball in court (company)')

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+',
                                   editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+', editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    # Soft delete: hidden from `objects`, kept (with its index row and audit trail) until purged
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+', editable=False)
    retain_until = models.DateField(null=True, blank=True, help_text='Not purged before this date.')
    legal_hold = models.BooleanField(default=False, help_text='Never purged while set.')
    # Optimistic lock: each save bumps it, and a save from a stale copy raises RecordConflict
    version = models.PositiveIntegerField(default=1, editable=False)

    objects = RecordManager()
    with_deleted = TenantManager()
    all_objects = models.Manager()

    class Meta(TenantModel.Meta):
        abstract = True
        default_manager_name = 'objects'
        unique_together = [('project', 'number')]

    # --- display --------------------------------------------------------------
    @property
    def reference(self):
        return self.ref or f'{get_record_type(self).prefix}-new'

    @property
    def is_shared(self):
        return self.visibility == VIS_SHARED

    @is_shared.setter
    def is_shared(self, value):
        self.visibility = VIS_SHARED if value else VIS_INTERNAL

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    @property
    def is_open(self):
        """Still waiting on whoever has the ball in court."""
        return not self.is_deleted and self.status in get_record_type(self).open_statuses

    # --- audit snapshot -------------------------------------------------------
    def _audit_values(self):
        deferred = self.get_deferred_fields()
        values = {}
        for name in get_record_type(self).audit_fields:
            f = self._meta.get_field(name)
            if f.attname not in deferred:
                values[name] = _json_value(getattr(self, f.attname))
        return values

    @classmethod
    def from_db(cls, db, field_names, values):
        obj = super().from_db(db, field_names, values)
        obj._audit_before = obj._audit_values()
        return obj

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._audit_before = self._audit_values()

    def _changes(self):
        before = getattr(self, '_audit_before', {})
        after = self._audit_values()
        if self._state.adding:
            return {k: [None, v] for k, v in after.items() if v not in (None, '', False)}
        return {k: [before[k], v] for k, v in after.items() if k in before and before[k] != v}

    # --- saving ---------------------------------------------------------------
    def save(self, *args, actor=None, audit_action=None, audit_note='', **kwargs):
        """
        Number it (first save), bump the version, save, write the audit entry and update the
        index, all in one transaction. actor is who did it (None for the system).
        """
        rt = get_record_type(self)
        if not self.organization_id:
            org = get_current_org()
            if org is None:
                raise ImproperlyConfigured(f'{type(self).__name__} saved with no organisation and no current org context.')
            self.organization = org
        if self.project_id and self.project.organization_id != self.organization_id:
            raise PermissionError(f'{type(self).__name__} saved against another organisation\'s project.')
        adding = self._state.adding
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            kwargs['update_fields'] = {*update_fields, 'version', 'updated_at', 'updated_by'}
        expected = self.version
        try:
            with transaction.atomic():
                if adding:
                    if actor is not None and not self.created_by_id:
                        self.created_by = actor
                    if not self.number:
                        self.number = next_number(self)
                if not self.ref:
                    self.ref = rt.format_ref(self.number)
                if actor is not None:
                    self.updated_by = actor
                changes = self._changes()
                if not adding:
                    self._expected_version = expected  # read by _do_update
                    self.version = expected + 1
                super().save(*args, **kwargs)
                self._expected_version = None
                idx = index_record(self)
                action = 'created' if adding else audit_action or ('updated' if changes else None)
                if action:
                    # Written with the record's organisation explicitly, so this works inside unscoped() too
                    AuditEntry.all_objects.create(
                        organization_id=self.organization_id, record_idx=idx, action=action, changes=changes,
                        actor=actor or (self.created_by if adding else None), note=audit_note[:255])
        except BaseException:
            self.version, self._expected_version = expected, None  # the transaction rolled back
            raise
        self._audit_before = self._audit_values()

    save.alters_data = True

    def _do_update(self, base_qs, using, pk_val, values, update_fields, forced_update):
        """The UPDATE only matches the version this copy was loaded at; anything else is a lost edit."""
        expected = getattr(self, '_expected_version', None)
        if expected is None:
            return super()._do_update(base_qs, using, pk_val, values, update_fields, forced_update)
        updated = super()._do_update(base_qs.filter(version=expected), using, pk_val, values, update_fields,
                                     forced_update)
        if not updated and base_qs.filter(pk=pk_val).exists():
            raise RecordConflict(f'{self.reference} was changed by someone else since it was opened.')
        return updated

    # --- soft delete, retention -----------------------------------------------
    def soft_delete(self, actor, note=''):
        self.deleted_at, self.deleted_by = timezone.now(), actor
        self.save(actor=actor, audit_action='deleted', audit_note=note)

    soft_delete.alters_data = True

    def restore(self, actor, note=''):
        self.deleted_at, self.deleted_by = None, None
        self.save(actor=actor, audit_action='restored', audit_note=note)

    restore.alters_data = True

    def check_purge(self):
        """Raise PurgeRefused unless the record may be removed for good now."""
        if not self.is_deleted:
            raise PurgeRefused(f'{self.reference} must be deleted before it is purged.')
        if self.legal_hold:
            raise PurgeRefused(f'{self.reference} is under legal hold.')
        if self.retain_until and self.retain_until > timezone.localdate():
            raise PurgeRefused(f'{self.reference} must be kept until {self.retain_until:%d %b %Y}.')

    def purge(self, actor, note=''):
        """
        Remove the row itself. Its index row stays (marked purged) and so does its audit trail,
        which is append-only; the 'purged' entry says who did it.
        """
        self.check_purge()
        with transaction.atomic():
            idx = RecordIndex.with_deleted_for(self)
            AuditEntry.all_objects.create(organization_id=self.organization_id, record_idx=idx, actor=actor,
                                          action='purged', changes={}, note=note[:255])
            idx.purged_at = timezone.now()
            idx.save()
            self._purging = True
            try:
                self.delete()
            finally:
                self._purging = False

    purge.alters_data = True

    def delete(self, *args, **kwargs):
        """Records are soft-deleted (soft_delete()) and later purged (purge()); a bare delete() is refused."""
        if not getattr(self, '_purging', False):
            raise PurgeRefused(f'Use soft_delete() or purge() on {type(self).__name__}, not delete().')
        return super().delete(*args, **kwargs)

    delete.alters_data = True


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

class RecordIndexManager(TenantManager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True, purged_at__isnull=True)


class RecordIndex(TenantModel):
    """
    One row per record of any type, written by RecordBase.save(): read this, not every
    register, for "Needs me", search and links. Use core.access.visible_index(request) in
    views so externals only get shared records on projects they can open.
    """
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name='+')
    object_id = models.PositiveBigIntegerField()
    record = GenericForeignKey('content_type', 'object_id')
    record_type = models.CharField(max_length=60, help_text='e.g. "rfis.rfi"')
    project = models.ForeignKey('projects.Project', on_delete=models.RESTRICT, related_name='record_index')
    ref = models.CharField(max_length=24, db_index=True)
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=20)
    status_label = models.CharField(max_length=60, blank=True)
    is_open = models.BooleanField(default=False, help_text='Waiting on the ball-in-court person.')
    ball_in_court_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                           related_name='+')
    ball_in_court_company = models.ForeignKey('directory.Subcontractor', null=True, blank=True,
                                              on_delete=models.SET_NULL, related_name='+')
    due_date = models.DateField(null=True, blank=True)
    visibility = models.CharField(max_length=8, choices=VISIBILITY_CHOICES)
    url = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    deleted_at = models.DateTimeField(null=True, blank=True)
    purged_at = models.DateTimeField(null=True, blank=True)

    objects = RecordIndexManager()  # live records only
    with_deleted = TenantManager()
    all_objects = models.Manager()

    class Meta(TenantModel.Meta):
        default_manager_name = 'objects'
        unique_together = [('content_type', 'object_id')]
        indexes = [
            models.Index(fields=['organization', 'ball_in_court_user', 'is_open'], name='core_index_needs_me'),
            models.Index(fields=['organization', 'project', 'record_type'], name='core_index_register'),
        ]
        verbose_name_plural = 'record index'

    def __str__(self):
        return f'{self.ref} {self.title}'

    @classmethod
    def with_deleted_for(cls, record):
        # Pinned by content type and pk to a row the caller already holds
        return cls.all_objects.get(content_type=ContentType.objects.get_for_model(record), object_id=record.pk)


def index_record(record):
    """Write the record's RecordIndex row. Called by RecordBase.save(); call it yourself after a queryset update()."""
    rt = get_record_type(record)
    ct = ContentType.objects.get_for_model(record)
    idx = RecordIndex.all_objects.filter(content_type=ct, object_id=record.pk).first() \
        or RecordIndex(organization_id=record.organization_id, content_type=ct, object_id=record.pk)
    idx.record_type = rt.key
    idx.project_id = record.project_id
    idx.ref, idx.title = record.ref, record.title[:200]
    idx.status, idx.status_label = record.status, str(record.get_status_display())[:60]
    idx.is_open = record.is_open
    idx.ball_in_court_user_id, idx.ball_in_court_company_id = record.ball_in_court_user_id, record.ball_in_court_company_id
    idx.due_date, idx.visibility = record.due_date, record.visibility
    idx.url = record.get_absolute_url()[:300] if hasattr(record, 'get_absolute_url') else ''
    idx.created_at, idx.updated_at = record.created_at, record.updated_at
    idx.deleted_at = record.deleted_at
    idx.save()
    return idx


# ---------------------------------------------------------------------------
# Links and audit
# ---------------------------------------------------------------------------

class AppendOnlyMixin:
    """Queryset writes that would rewrite history are refused. Django's delete cascades (a whole workspace) still run."""

    def update(self, **kwargs):
        raise PermissionError(f'{self.model.__name__} rows are append-only.')

    update.alters_data = True

    def delete(self):
        raise PermissionError(f'{self.model.__name__} rows are append-only.')

    delete.alters_data = True
    delete.queryset_only = True

    def bulk_update(self, *args, **kwargs):
        raise PermissionError(f'{self.model.__name__} rows are append-only.')

    bulk_update.alters_data = True


class AppendOnlyQuerySet(AppendOnlyMixin, models.QuerySet):
    pass


class TenantAppendOnlyQuerySet(AppendOnlyMixin, TenantQuerySet):
    pass


class AuditEntry(TenantModel):
    """
    What happened to a record, by whom, with the audited fields' old and new values as
    {"field": [old, new]}. Append-only: save() refuses to change an existing entry and
    delete() refuses outright, on the instance and the queryset. Deleting a whole workspace
    still removes them, through Django's cascade. With Postgres we can add a trigger that
    refuses UPDATE/DELETE in the database too (see the ADR).
    """
    record_idx = models.ForeignKey(RecordIndex, on_delete=models.RESTRICT, related_name='audit')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+')
    action = models.CharField(max_length=40)  # created, updated, deleted, restored, purged, linked, a transition name
    changes = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=255, blank=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = TenantManager.from_queryset(TenantAppendOnlyQuerySet)()
    all_objects = models.Manager.from_queryset(AppendOnlyQuerySet)()

    class Meta(TenantModel.Meta):
        ordering = ['at', 'pk']
        verbose_name_plural = 'audit entries'

    def __str__(self):
        return f'{self.actor or "System"} {self.action} {self.record_idx_id} at {self.at:%Y-%m-%d %H:%M}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError('Audit entries are append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError('Audit entries are append-only.')


class RecordLink(TenantModel):
    """
    "VO-003 came from RFI-007". Links are never edited: make one with link() and remove it with
    unlink(); both write an AuditEntry on each end, so the history of a link is append-only.
    """
    RELATES = 'relates'
    DERIVED_FROM = 'derived_from'
    SUPERSEDES = 'supersedes'
    KIND_CHOICES = [(RELATES, 'Relates to'), (DERIVED_FROM, 'Came from'), (SUPERSEDES, 'Supersedes')]

    from_idx = models.ForeignKey(RecordIndex, on_delete=models.CASCADE, related_name='links_out')
    to_idx = models.ForeignKey(RecordIndex, on_delete=models.CASCADE, related_name='links_in')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=RELATES)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager.from_queryset(TenantAppendOnlyQuerySet)()
    all_objects = models.Manager.from_queryset(AppendOnlyQuerySet)()

    class Meta(TenantModel.Meta):
        unique_together = [('from_idx', 'to_idx', 'kind')]

    def __str__(self):
        return f'{self.from_idx} {self.get_kind_display().lower()} {self.to_idx}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError('Record links are not edited; unlink() and link() again.')
        if self.from_idx_id == self.to_idx_id:
            raise ValueError('A record cannot link to itself.')
        org_ids = {self.from_idx.organization_id, self.to_idx.organization_id}
        if len(org_ids) > 1 or (self.organization_id and self.organization_id not in org_ids):
            raise PermissionError('Records can only be linked within one organisation.')
        self.organization_id = self.organization_id or self.from_idx.organization_id
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not getattr(self, '_unlinking', False):
            raise PermissionError('Remove a link with core.models.unlink(), which records it in the audit trail.')
        return super().delete(*args, **kwargs)


def _idx(record_or_idx):
    return record_or_idx if isinstance(record_or_idx, RecordIndex) else RecordIndex.with_deleted_for(record_or_idx)


def link(source, target, kind=RecordLink.RELATES, actor=None):
    """Link two records (or their index rows). Linking twice is harmless."""
    a, b = _idx(source), _idx(target)
    with transaction.atomic():
        found = RecordLink.all_objects.filter(from_idx=a, to_idx=b, kind=kind).first()
        if found:
            return found
        made = RecordLink(organization_id=a.organization_id, from_idx=a, to_idx=b, kind=kind, created_by=actor)
        made.save()
        for idx, other in ((a, b), (b, a)):
            AuditEntry.all_objects.create(organization_id=idx.organization_id, record_idx=idx, actor=actor,
                                          action='linked', changes={'link': [None, f'{kind} {other.ref}']},
                                          note=str(made)[:255])
        return made


def unlink(source, target, kind=RecordLink.RELATES, actor=None):
    a, b = _idx(source), _idx(target)
    with transaction.atomic():
        found = RecordLink.all_objects.filter(from_idx=a, to_idx=b, kind=kind).first()
        if not found:
            return False
        note = str(found)[:255]
        found._unlinking = True
        found.delete()
        for idx, other in ((a, b), (b, a)):
            AuditEntry.all_objects.create(organization_id=idx.organization_id, record_idx=idx, actor=actor,
                                          action='unlinked', changes={'link': [f'{kind} {other.ref}', None]}, note=note)
        return True
