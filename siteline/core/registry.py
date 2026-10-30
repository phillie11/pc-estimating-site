"""
Which models are records, and how each behaves.

Every RecordBase subclass registers itself from its AppConfig.ready():

    register_record_type(RFI, prefix='RFI', transitions=TRANSITIONS, open_statuses=(RFI.STATUS_OPEN,),
                         audit_fields=('question', 'response', ...))

RecordBase.save() looks the type up here for its ref prefix, which statuses still
need the ball-in-court person to act ("Needs me"), and which fields are audited.
An unregistered RecordBase subclass refuses to save.
"""
from dataclasses import dataclass, field

from django.core.exceptions import ImproperlyConfigured

# Audited on every record type: the fields RecordIndex mirrors plus the retention fields
BASE_AUDIT_FIELDS = ('title', 'status', 'visibility', 'due_date', 'ball_in_court_user', 'ball_in_court_company',
                     'legal_hold', 'retain_until', 'deleted_at')


@dataclass(frozen=True)
class RecordType:
    model: type
    prefix: str
    transitions: dict = field(default_factory=dict)  # name -> Transition
    open_statuses: tuple = ()  # statuses where the ball-in-court person has something to do
    audit_fields: tuple = BASE_AUDIT_FIELDS
    draft_statuses: tuple = ()  # not yet issued: staff may delete their own (core.access.can_soft_delete)

    @property
    def key(self):
        """Stable name for numbering and the index ('rfis.rfi'); unlike the prefix it never changes."""
        return self.model._meta.label_lower

    @property
    def label(self):
        return self.model._meta.verbose_name

    def format_ref(self, number):
        return f'{self.prefix}-{number:03d}'


_registry = {}


def register_record_type(model, *, prefix, transitions=(), open_statuses=(), audit_fields=(), draft_statuses=()):
    from .models import RecordBase
    if not (isinstance(model, type) and issubclass(model, RecordBase)):
        raise ImproperlyConfigured(f'{model!r} must subclass core.models.RecordBase to be registered.')
    if any(rt.prefix == prefix and rt.model is not model for rt in _registry.values()):
        raise ImproperlyConfigured(f'Record prefix {prefix!r} is already used.')
    statuses = {value for value, _ in model._meta.get_field('status').choices or ()}
    by_name = {}
    for t in transitions:
        if t.name in by_name:
            raise ImproperlyConfigured(f'{model.__name__} declares transition {t.name!r} twice.')
        unknown = ({t.target, *t.source} - {'*'}) - statuses
        if statuses and unknown:
            raise ImproperlyConfigured(f'{model.__name__} transition {t.name!r} uses unknown statuses {sorted(unknown)}.')
        by_name[t.name] = t
    for name in audit_fields:
        model._meta.get_field(name)  # FieldDoesNotExist now rather than on the first save
    rt = RecordType(model=model, prefix=prefix, transitions=by_name, open_statuses=tuple(open_statuses),
                    audit_fields=tuple(dict.fromkeys((*BASE_AUDIT_FIELDS, *audit_fields))),
                    draft_statuses=tuple(draft_statuses))
    _registry[model] = rt
    return rt


def get_record_type(model_or_record):
    model = model_or_record if isinstance(model_or_record, type) else type(model_or_record)
    model = model._meta.concrete_model
    try:
        return _registry[model]
    except KeyError:
        raise ImproperlyConfigured(
            f'{model.__name__} subclasses RecordBase but was never registered. Call '
            'core.registry.register_record_type() from its AppConfig.ready().'
        ) from None


def record_types():
    return list(_registry.values())
