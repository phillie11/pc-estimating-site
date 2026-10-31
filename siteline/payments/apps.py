from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """Interim applications for payment and the Construction Act payment cycle. See payments/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payments'
    verbose_name = 'Applications for payment'

    def ready(self):
        import actions
        from actions.providers import HookProvider
        from core.registry import register_record_type

        from .models import Application
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        register_record_type(Application, prefix='AP', transitions=TRANSITIONS,
                             open_statuses=(Application.STATUS_SUBMITTED,), draft_statuses=(Application.STATUS_DRAFT,),
                             audit_fields=AUDIT_FIELDS,
                             # The client only: never consultants or subcontractors
                             external_roles=('CLIENT',))
        # "Needs me": payments.actions.items lists the contractor's and the client's items itself, so it
        # covers the index rows (the client would otherwise see a submitted application twice)
        actions.register(HookProvider('payment', 'Applications for payment', 'payments.actions.items',
                                      covers=('payments.application',)))
