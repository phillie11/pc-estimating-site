from django.apps import AppConfig


class ChangesConfig(AppConfig):
    """Early warnings and change events (internal). See changes/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'changes'

    def ready(self):
        from core.registry import register_record_type

        from .models import ChangeEvent, EarlyWarning
        from .workflow import (CHANGE_EVENT_AUDIT, CHANGE_EVENT_TRANSITIONS, EARLY_WARNING_AUDIT,
                               EARLY_WARNING_TRANSITIONS)
        register_record_type(EarlyWarning, prefix='EW', transitions=EARLY_WARNING_TRANSITIONS,
                             open_statuses=(EarlyWarning.STATUS_OPEN,), draft_statuses=(EarlyWarning.STATUS_OPEN,),
                             audit_fields=EARLY_WARNING_AUDIT, external_roles=())
        register_record_type(ChangeEvent, prefix='CE', transitions=CHANGE_EVENT_TRANSITIONS,
                             open_statuses=ChangeEvent.LIVE_STATUSES, draft_statuses=(ChangeEvent.STATUS_IDENTIFIED,),
                             audit_fields=CHANGE_EVENT_AUDIT, external_roles=())
