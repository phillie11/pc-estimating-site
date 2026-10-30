from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'documents'
    verbose_name = 'Document control'

    def ready(self):
        from core.registry import register_record_type

        from .models import Transmittal
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        # Issued: waiting on the recipients to acknowledge receipt
        register_record_type(Transmittal, prefix='TR', transitions=TRANSITIONS,
                             open_statuses=(Transmittal.STATUS_ISSUED,),
                             draft_statuses=(Transmittal.STATUS_DRAFT,), audit_fields=AUDIT_FIELDS)
