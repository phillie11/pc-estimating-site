"""
Reading and keeping the change chain's core RecordLinks.

Direction: the later record links to the one it came from, kind DERIVED_FROM ("came from"):
    CE-001 came from RFI-007        CE-001 came from EW-002        VO-003 came from CE-001
    VO-003 came from RFI-007 (kept in step with Variation.rfi while that FK stays)
Reads go through RecordLink.objects / RecordIndex rows, both scoped to the current organisation.
"""
from core.models import RecordIndex, RecordLink, link, unlink

DERIVED = RecordLink.DERIVED_FROM


def _idx(record):
    return RecordIndex.with_deleted_for(record)


def came_from(record, record_type=None):
    """Index rows of the records this one came from (optionally of one type, e.g. 'rfis.rfi')."""
    qs = RecordLink.objects.filter(from_idx=_idx(record), kind=DERIVED).select_related('to_idx')
    if record_type:
        qs = qs.filter(to_idx__record_type=record_type)
    return [lk.to_idx for lk in qs.order_by('pk') if lk.to_idx.purged_at is None]


def led_to(record, record_type=None):
    """Index rows of the records that came from this one."""
    qs = RecordLink.objects.filter(to_idx=_idx(record), kind=DERIVED).select_related('from_idx')
    if record_type:
        qs = qs.filter(from_idx__record_type=record_type)
    return [lk.from_idx for lk in qs.order_by('pk') if lk.from_idx.purged_at is None]


def record_of(idx):
    """The live record behind an index row (None if soft-deleted or gone)."""
    model = idx.content_type.model_class()
    return model.objects.filter(pk=idx.object_id).first() if model else None


def set_source(record, source, record_type, actor=None):
    """
    Make `source` the one record of `record_type` this record came from: link it and unlink any
    other of that type. source=None removes them all. Returns True if anything changed.
    """
    changed = False
    keep = _idx(source).pk if source is not None else None
    for idx in came_from(record, record_type):
        if idx.pk != keep:
            unlink(record, idx, DERIVED, actor=actor)
            changed = True
    if source is not None and keep not in {i.pk for i in came_from(record, record_type)}:
        link(record, source, DERIVED, actor=actor)
        changed = True
    return changed
