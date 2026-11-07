from django.apps import AppConfig


class QualityConfig(AppConfig):
    """Inspections (INS-), permits to work (PTW-) and the fire-stopping register (FS-). See quality/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'quality'
    verbose_name = 'Quality'

    def ready(self):
        from actions.providers import register_record_formatter
        from core.registry import register_record_type

        from . import workflow as w
        from .actions import TYPES
        from .actions import format as format_item
        from .models import Inspection, Penetration, Permit
        # All internal in the index (external_roles=()): subs reach their own through quality/portal.py
        register_record_type(Inspection, prefix='INS', transitions=w.INSPECTION_TRANSITIONS,
                             open_statuses=Inspection.OPEN_STATUSES, audit_fields=w.INSPECTION_AUDIT_FIELDS,
                             draft_statuses=(Inspection.IN_PROGRESS,), external_roles=())
        register_record_type(Permit, prefix='PTW', transitions=w.PERMIT_TRANSITIONS,
                             open_statuses=Permit.OPEN_STATUSES, audit_fields=w.PERMIT_AUDIT_FIELDS,
                             external_roles=())
        register_record_type(Penetration, prefix='FS', transitions=w.PENETRATION_TRANSITIONS,
                             open_statuses=Penetration.OPEN_STATUSES, audit_fields=w.PENETRATION_AUDIT_FIELDS,
                             draft_statuses=(Penetration.TO_SEAL,), external_roles=())
        # Needs me: worded by quality.actions.format from the records provider's one index query
        for record_type in TYPES:
            register_record_formatter(record_type, format_item)
        # A subcontractor's failed seals, in their portal (the record index never serves externals)
        import actions
        from actions.providers import HookProvider
        actions.register(HookProvider('firestopping_sub', 'Fire-stopping to put right', 'quality.portal.sub_items',
                                      staff=False))
