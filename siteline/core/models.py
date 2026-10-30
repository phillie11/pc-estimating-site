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
  AuditEntry      append-only history per record with field-level diffs; redact() is the one
                  sanctioned change (GDPR erasure).
  RetentionPolicy how many years after project completion an organisation keeps records.

Everything here is a TenantModel, so it is scoped to the current organisation and fails closed.
"""
import contextvars
import datetime
import decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, OperationalError, models, transaction
from django.db.models import F, Max
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.db.models.fields.files import FieldFile
from django.utils import timezone

from tenancy.context import get_current_org
from tenancy.models import TenantManager, TenantModel, TenantQuerySet

from .exceptions import PurgeRefused, RecordConflict
from .registry import get_record_type, record_types

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


def _locked_first(qs):
    """The first row of qs, locked. order_by() keeps the lock to the row itself (no ORDER BY pk)."""
    return next(iter(qs.select_for_update().order_by()[:1]), None)


def _is_deadlock(exc):
    return 'deadlock' in str(exc).lower() or getattr(exc, 'args', (None,))[0] == 1213  # 1213: MySQL


def _take_number(record, rows, explicit):
    if rows.exists():
        # The usual case: lock the existing row by itself, without a range lock on the index
        seq = _locked_first(rows)
    else:
        seq = None
    if seq is None:
        # First of its type on this project (or our read predates the row): only this branch
        # serialises on the project row, so two first RFIs can't both create the sequence or
        # deadlock on MySQL's gap locks. Re-read with a locking read, which sees the latest commit.
        from projects.models import Project
        list(Project.all_objects.select_for_update().filter(pk=record.project_id).order_by().values_list('pk'))
        seq = _locked_first(rows)
    if seq is None:
        used = type(record).all_objects.filter(project_id=record.project_id).aggregate(m=Max('number'))['m'] or 0
        try:
            with transaction.atomic():  # savepoint: someone else may still create it first
                seq = NumberSequence(organization_id=record.organization_id, project_id=record.project_id,
                                     key=get_record_type(record).key, next_value=used + 1)
                seq.save()
        except IntegrityError:
            seq = rows.select_for_update().get()
    if explicit:
        # A number set by hand: take it, and make sure the sequence never hands it out again
        number = explicit
        seq.next_value = max(seq.next_value, explicit + 1)
    else:
        number = seq.next_value
        seq.next_value = number + 1
    seq.save(update_fields=['next_value'])
    return number


def next_number(record, explicit=None):
    """
    Take the next number for this record's type and project, or record that `explicit` (a number
    set by hand) is used. Call inside the transaction that saves the record, so a failed save gives
    the number back. The first record of a type on a project creates the sequence, starting after
    any number already used (rows that predate it). A deadlock is retried once from a savepoint;
    on MySQL a deadlock rolls back the whole transaction, so there the retry fails loudly instead.
    """
    # Pinned by organisation and project, so all_objects can't reach another company's sequence
    rows = NumberSequence.all_objects.filter(organization_id=record.organization_id, project_id=record.project_id,
                                             key=get_record_type(record).key)
    for attempt in (1, 2):
        try:
            with transaction.atomic():
                return _take_number(record, rows, explicit)
        except OperationalError as e:
            if attempt == 2 or not _is_deadlock(e):
                raise


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def add_years(day, years):
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February into a non-leap year
        return day.replace(year=day.year + years, day=28)


class RetentionPolicy(TenantModel):
    """
    How long an organisation keeps records after its project completes. With no row the
    default is settings.RECORD_RETENTION_YEARS (12 if unset). Kept in core rather than on
    Organization so the policy can grow (per record type, per sector) without touching tenancy.
    """
    retention_years = models.PositiveSmallIntegerField(
        help_text='Records are kept this many years after their project completes.')

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['organization'], name='core_one_retention_policy_per_org')]
        verbose_name_plural = 'retention policies'

    def __str__(self):
        return f'{self.retention_years} years'

    @staticmethod
    def default_years():
        return getattr(settings, 'RECORD_RETENTION_YEARS', 12)

    @classmethod
    def years_for(cls, org_id):
        # Pinned to the organisation the caller already holds
        years = cls.all_objects.filter(organization_id=org_id).values_list('retention_years', flat=True).first()
        return cls.default_years() if years is None else years


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
        saved = None  # audited field names this save writes; None for all of them
        if update_fields is not None:
            saved = {self._meta.get_field(name).name for name in update_fields}
            kwargs['update_fields'] = {*update_fields, 'version', 'updated_at', 'updated_by'}
        expected, before = self.version, (self.pk, self.number, self.ref)
        try:
            with transaction.atomic():
                if adding:
                    if actor is not None and not self.created_by_id:
                        self.created_by = actor
                    # A number set by hand is kept, and the sequence moves past it
                    self.number = next_number(self, explicit=self.number or None)
                if not self.ref:
                    self.ref = rt.format_ref(self.number)
                if actor is not None:
                    self.updated_by = actor
                changes = self._changes()
                if saved is not None:
                    changes = {k: v for k, v in changes.items() if k in saved}
                if 'legal_hold' in changes:
                    from .access import can_set_legal_hold
                    if not can_set_legal_hold(actor, self.organization_id):
                        raise PermissionDenied('Only owners and admins can set or clear a legal hold.')
                if not adding:
                    self._expected_version = expected  # read by _do_update
                    self.version = expected + 1
                super().save(*args, **kwargs)
                self._expected_version = None
                idx = index_record(self)
                action = 'created' if adding else audit_action or ('updated' if changes else None)
                if action:
                    # Written with the record's organisation explicitly, so this works inside unscoped() too
                    AuditEntry.write(self.organization_id, idx, action, actor or (self.created_by if adding else None),
                                     changes=changes, note=audit_note)
        except BaseException:
            # The transaction rolled back: put the copy back as it was, so a retry is a clean first save
            self.version, self._expected_version = expected, None
            if adding:
                self.pk, self.number, self.ref = before
                self._state.adding = True
            raise
        after = self._audit_values()
        if saved is None:
            self._audit_before = after
        else:  # fields this save didn't write still count as changed next time
            self._audit_before = {**getattr(self, '_audit_before', {}), **{k: after[k] for k in saved if k in after}}

    save.alters_data = True

    def _do_update(self, base_qs, using, pk_val, values, update_fields, forced_update):
        """
        The UPDATE only matches the version this copy was loaded at. Matching nothing means someone
        else saved it, or it was purged: either way refuse, and never fall through to Django's INSERT,
        which would bring a purged record back.
        """
        expected = getattr(self, '_expected_version', None)
        if expected is None:
            return super()._do_update(base_qs, using, pk_val, values, update_fields, forced_update)
        if not super()._do_update(base_qs.filter(version=expected), using, pk_val, values, update_fields,
                                  forced_update):
            raise RecordConflict(f'{self.reference} was changed or removed by someone else since it was opened.')
        return True

    # --- soft delete, retention -----------------------------------------------
    # Who may do these is decided in core.access (can_soft_delete, can_restore, can_purge,
    # can_set_legal_hold), so the rules live in one place.
    def soft_delete(self, actor, note=''):
        from .access import can_soft_delete
        if not can_soft_delete(actor, self):
            raise PermissionDenied(f'You cannot delete {self.reference}.')
        self.deleted_at, self.deleted_by = timezone.now(), actor
        self.save(actor=actor, audit_action='deleted', audit_note=note)

    soft_delete.alters_data = True

    def restore(self, actor, note=''):
        from .access import can_restore
        if not can_restore(actor, self):
            raise PermissionDenied(f'You cannot restore {self.reference}.')
        self.deleted_at, self.deleted_by = None, None
        self.save(actor=actor, audit_action='restored', audit_note=note)

    restore.alters_data = True

    def effective_retain_until(self):
        """
        retain_until if set by hand, else the project's completion date plus the organisation's
        retention period (RetentionPolicy). None while the project has no completion date.
        """
        if self.retain_until:
            return self.retain_until
        completion = self.project.completion_date
        if completion is None:
            return None
        return add_years(completion, RetentionPolicy.years_for(self.organization_id))

    def check_purge(self):
        """Raise PurgeRefused unless the record may be removed for good now. Nothing purges automatically."""
        if not self.is_deleted:
            raise PurgeRefused(f'{self.reference} must be deleted before it is purged.')
        if self.legal_hold:
            raise PurgeRefused(f'{self.reference} is under legal hold.')
        keep_until = self.effective_retain_until()
        if keep_until is None:
            raise PurgeRefused(f'{self.reference} has no retention date yet: its project has no completion date.')
        if keep_until > timezone.localdate():
            raise PurgeRefused(f'{self.reference} must be kept until {keep_until:%d %b %Y}.')

    def purge(self, actor, note=''):
        """
        Remove the row itself (owners only). Its index row stays (marked purged) and so does its
        audit trail, which is append-only; the 'purged' entry says who did it.
        """
        from .access import can_purge
        if not can_purge(actor, self):
            raise PermissionDenied('Only owners can purge records.')
        self.check_purge()
        with transaction.atomic():
            idx = RecordIndex.with_deleted_for(self)
            AuditEntry.write(self.organization_id, idx, 'purged', actor, note=note)
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

# Set only inside redact(): the one sanctioned way to change audit rows (GDPR erasure)
_redacting = contextvars.ContextVar('core_redacting', default=False)
REDACTED = '[redacted]'


def _nulls_user_fks(model, kwargs):
    """True for the UPDATE Django's delete collector runs when a user is deleted: user FKs set to None."""
    User = get_user_model()
    for name, value in kwargs.items():
        f = model._meta.get_field(name)
        if value is not None or not (f.is_relation and f.related_model is User):
            return False
    return bool(kwargs)


class AppendOnlyMixin:
    """
    Queryset writes that would rewrite history are refused. Two exceptions: deleting a user sets
    their FKs to NULL (the actor's name stays in actor_name), and redact() is let through.
    Django's delete cascades (a whole workspace) still run, as they use _raw_delete.
    """

    def update(self, **kwargs):
        if _redacting.get() or _nulls_user_fks(self.model, kwargs):
            return super().update(**kwargs)
        raise PermissionError(f'{self.model.__name__} rows are append-only.')

    update.alters_data = True

    def delete(self):
        raise PermissionError(f'{self.model.__name__} rows are append-only.')

    delete.alters_data = True
    delete.queryset_only = True

    def bulk_update(self, *args, **kwargs):
        raise PermissionError(f'{self.model.__name__} rows are append-only.')

    bulk_update.alters_data = True

    def bulk_create(self, objs, *args, **kwargs):
        # bulk_create(objs, batch_size, ignore_conflicts, update_conflicts, ...): an upsert would overwrite rows
        if kwargs.get('update_conflicts') or (len(args) >= 3 and args[2]):
            raise PermissionError(f'{self.model.__name__} rows are append-only: no update_conflicts.')
        return super().bulk_create(objs, *args, **kwargs)

    bulk_create.alters_data = True


class AppendOnlyQuerySet(AppendOnlyMixin, models.QuerySet):
    pass


class TenantAppendOnlyQuerySet(AppendOnlyMixin, TenantQuerySet):
    pass


def _actor_name(user):
    return ((getattr(user, 'full_name', '') or getattr(user, 'email', '')) if user else '')[:150]


class AuditEntry(TenantModel):
    """
    What happened to a record, by whom, with the audited fields' old and new values as
    {"field": [old, new]}. Append-only: save() always INSERTs (an existing pk is an IntegrityError),
    delete() refuses, and so do queryset update(), delete(), bulk_update() and upserts; loaddata
    can't overwrite an existing row either. actor_name keeps who acted after the user is deleted.
    The only sanctioned change is redact() (GDPR erasure). Deleting a whole workspace still removes
    them, through Django's cascade. Postgres triggers can enforce this in the database (see the ADR).
    """
    record_idx = models.ForeignKey(RecordIndex, on_delete=models.RESTRICT, related_name='audit')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+')
    actor_name = models.CharField(max_length=150, blank=True, help_text='Who acted, as named at the time.')
    action = models.CharField(max_length=40)  # created, updated, deleted, restored, purged, linked, redacted, a transition
    changes = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=255, blank=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = TenantManager.from_queryset(TenantAppendOnlyQuerySet)()
    all_objects = models.Manager.from_queryset(AppendOnlyQuerySet)()

    class Meta(TenantModel.Meta):
        ordering = ['at', 'pk']
        verbose_name_plural = 'audit entries'

    def __str__(self):
        return f'{self.actor_name or "System"} {self.action} {self.record_idx_id} at {self.at:%Y-%m-%d %H:%M}'

    @classmethod
    def write(cls, org_id, idx, action, actor, changes=None, note=''):
        """Add an entry. The organisation is explicit, so this works inside unscoped() too."""
        entry = cls(organization_id=org_id, record_idx=idx, action=action, actor=actor, actor_name=_actor_name(actor),
                    changes=changes or {}, note=note[:255])
        entry.save()
        return entry

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError('Audit entries are append-only.')
        if self.actor_id and not self.actor_name:
            self.actor_name = _actor_name(self.actor)
        kwargs['force_insert'] = True  # never an UPDATE, even for AuditEntry(pk=<existing>)
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
        return f'{self.from_idx.ref} {self.get_kind_display().lower()} {self.to_idx.ref}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError('Record links are not edited; unlink() and link() again.')
        if self.from_idx_id == self.to_idx_id:
            raise ValueError('A record cannot link to itself.')
        org_ids = {self.from_idx.organization_id, self.to_idx.organization_id}
        if len(org_ids) > 1 or (self.organization_id and self.organization_id not in org_ids):
            raise PermissionError('Records can only be linked within one organisation.')
        self.organization_id = self.organization_id or self.from_idx.organization_id
        kwargs['force_insert'] = True  # never an UPDATE, even for RecordLink(pk=<existing>)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not getattr(self, '_unlinking', False):
            raise PermissionError('Remove a link with core.models.unlink(), which records it in the audit trail.')
        return super().delete(*args, **kwargs)


@receiver(pre_save, sender=AuditEntry)
@receiver(pre_save, sender=RecordLink)
def _refuse_raw_overwrite(sender, instance, raw, **kwargs):
    """
    loaddata / import_data save raw, bypassing save(), and would UPDATE a row that already exists.
    Re-loading a row exactly as it is changes nothing and is allowed; any different value is refused.
    """
    if not raw or instance.pk is None:
        return

    def same(a, b):
        if isinstance(a, datetime.datetime) and isinstance(b, datetime.datetime):
            return abs(a - b) < datetime.timedelta(milliseconds=1)  # JSON dumps keep milliseconds only
        return a == b

    existing = sender._base_manager.filter(pk=instance.pk).first()
    if existing is not None and not all(same(getattr(existing, f.attname), getattr(instance, f.attname))
                                        for f in sender._meta.concrete_fields):
        raise PermissionError(f'{sender.__name__} {instance.pk} already exists; append-only rows are not overwritten.')


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
        # Refs only, never titles: the other record may be internal, or erased later
        for idx, other in ((a, b), (b, a)):
            AuditEntry.write(idx.organization_id, idx, 'linked', actor,
                             changes={'link': [None, f'{kind} {other.ref}']}, note=str(made))
        return made


def unlink(source, target, kind=RecordLink.RELATES, actor=None):
    a, b = _idx(source), _idx(target)
    with transaction.atomic():
        found = RecordLink.all_objects.filter(from_idx=a, to_idx=b, kind=kind).first()
        if not found:
            return False
        note = str(found)
        found._unlinking = True
        found.delete()
        for idx, other in ((a, b), (b, a)):
            AuditEntry.write(idx.organization_id, idx, 'unlinked', actor,
                             changes={'link': [f'{kind} {other.ref}', None]}, note=note)
        return True


# ---------------------------------------------------------------------------
# Redaction (GDPR erasure)
# ---------------------------------------------------------------------------

def _scrub(value, needles):
    if isinstance(value, str):
        for n in needles:
            value = value.replace(n, REDACTED)
    return value


def redact(record_or_user, actor, reason):
    """
    Erase personal data from the audit trail and the index. The only sanctioned way to change
    audit rows; it records itself with a 'redacted' entry (the reason as its note, no values).

    A record: every old and new value in its audit entries becomes "[redacted]" (the field names
    stay, so the trail still shows what changed and when), and so does its title, on the record
    and in the index.

    A user: entries they made lose the actor link and name, values that pointed at them (ball in
    court and other user fields) or contain their name or email become "[redacted]", and so do
    those strings in record and index titles. Then delete the user as usual.

    Other free text on a live record (an RFI's question) is not touched: edit or purge the record.
    Works in the current organisation; inside unscoped() it covers every organisation. The actor
    must be an owner or admin of each organisation touched, or a superuser (core.access.can_redact).
    Returns the number of audit entries changed.
    """
    from .access import can_redact
    if not reason:
        raise ValueError('Say why the data is being redacted.')
    if isinstance(record_or_user, get_user_model()):
        return _redact_user(record_or_user, actor, reason, can_redact)
    record = record_or_user
    if not can_redact(actor, record.organization_id):
        raise PermissionDenied('Only owners and admins can redact records.')
    with transaction.atomic():
        idx = RecordIndex.with_deleted_for(record)
        changed = 0
        token = _redacting.set(True)
        try:
            for entry in AuditEntry.all_objects.filter(record_idx=idx):
                scrubbed = {k: [None if v is None else REDACTED for v in pair] for k, pair in entry.changes.items()}
                if scrubbed != entry.changes:
                    AuditEntry.all_objects.filter(pk=entry.pk).update(changes=scrubbed)
                    changed += 1
        finally:
            _redacting.reset(token)
        # Straight updates, so the redaction itself doesn't write the old title into a new diff
        type(record).all_objects.filter(pk=record.pk).update(title=REDACTED, version=F('version') + 1)
        RecordIndex.all_objects.filter(pk=idx.pk).update(title=REDACTED)
        AuditEntry.write(idx.organization_id, idx, 'redacted', actor, note=reason)
    record.refresh_from_db()
    return changed


def _redact_user(user, actor, reason, can_redact):
    User = get_user_model()
    user_fields = {f.name for rt in record_types() for f in rt.model._meta.concrete_fields
                   if f.is_relation and f.related_model is User}
    needles = sorted({s for s in (user.full_name, user.email) if s}, key=len, reverse=True)
    touched = {}
    for entry in AuditEntry.objects.select_related('record_idx'):  # current org, or all inside unscoped()
        changes = {name: [REDACTED if name in user_fields and v == user.pk else _scrub(v, needles) for v in pair]
                   for name, pair in entry.changes.items()}
        mine = entry.actor_id == user.pk
        if mine or changes != entry.changes:
            touched[entry.pk] = (entry, changes, mine)
    titles = [i for i in RecordIndex.with_deleted.all() if any(n in i.title for n in needles)]
    for org_id in {e.organization_id for e, _, _ in touched.values()} | {i.organization_id for i in titles}:
        if not can_redact(actor, org_id):
            raise PermissionDenied('Only owners and admins can redact.')
    with transaction.atomic():
        token = _redacting.set(True)
        try:
            for entry, changes, mine in touched.values():
                fields = {'changes': changes, **({'actor': None, 'actor_name': REDACTED} if mine else {})}
                AuditEntry.all_objects.filter(pk=entry.pk).update(**fields)
        finally:
            _redacting.reset(token)
        for idx in titles:
            title = _scrub(idx.title, needles)
            RecordIndex.all_objects.filter(pk=idx.pk).update(title=title)
            idx.content_type.model_class().all_objects.filter(pk=idx.object_id) \
                .update(title=title, version=F('version') + 1)
        done = {e.record_idx_id: e.record_idx for e, _, _ in touched.values()}
        done.update({i.pk: i for i in titles})
        for idx in done.values():
            AuditEntry.write(idx.organization_id, idx, 'redacted', actor, note=reason)
    return len(touched)
