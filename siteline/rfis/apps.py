from django.apps import AppConfig


class RfisConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rfis"

    def ready(self):
        from core.registry import register_record_type

        from .models import RFI
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        # Only an open RFI is waiting on its ball-in-court person; a responded one is back with the team
        register_record_type(RFI, prefix='RFI', transitions=TRANSITIONS, open_statuses=(RFI.STATUS_OPEN,),
                             audit_fields=AUDIT_FIELDS)
