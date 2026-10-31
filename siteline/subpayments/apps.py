from django.apps import AppConfig


class SubpaymentsConfig(AppConfig):
    """Paying subcontractors: terms, applications, our notices, CIS, payments, retention. See subpayments/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'subpayments'
    verbose_name = 'Subcontractor payments'

    def ready(self):
        import actions
        from actions.providers import HookProvider
        from core.registry import register_record_type

        from .models import SubApplication
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        register_record_type(SubApplication, prefix='SA', transitions=TRANSITIONS,
                             open_statuses=(SubApplication.RECEIVED,), audit_fields=AUDIT_FIELDS,
                             # Each sub sees only their own, which the index can't express: never through it
                             external_roles=())
        # "Needs me": staff deadlines and the sub's "apply for payment"; covers the index rows
        actions.register(HookProvider('subpayment', 'Subcontractor payments', 'subpayments.actions.items',
                                      covers=('subpayments.subapplication',)))
