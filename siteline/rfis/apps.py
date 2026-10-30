from django.apps import AppConfig


class RfisConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rfis"

    def ready(self):
        from core.registry import register_record_type

        from .models import RFI
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        # Open: waiting on whoever must answer. Responded: back with whoever raised it, to accept or reopen.
        register_record_type(RFI, prefix='RFI', transitions=TRANSITIONS,
                             open_statuses=(RFI.STATUS_OPEN, RFI.STATUS_RESPONDED),
                             draft_statuses=(RFI.STATUS_DRAFT,), audit_fields=AUDIT_FIELDS)
