from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'documents'
    verbose_name = 'Document control'

    def ready(self):
        from django.db.models.signals import post_save

        from core.registry import register_record_type
        from projects.models import DocumentRevision

        from .models import Transmittal
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        # Issued: waiting on the recipients to acknowledge receipt
        register_record_type(Transmittal, prefix='TR', transitions=TRANSITIONS,
                             open_statuses=(Transmittal.STATUS_ISSUED,),
                             draft_statuses=(Transmittal.STATUS_DRAFT,), audit_fields=AUDIT_FIELDS)

        # Searching inside PDFs: read each new PDF revision's words once the upload commits (textindex.py)
        post_save.connect(_revision_saved, sender=DocumentRevision, dispatch_uid='documents.textindex')


def _revision_saved(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        from . import textindex
        textindex.queue(instance)
