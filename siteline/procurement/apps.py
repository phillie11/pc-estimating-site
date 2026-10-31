from django.apps import AppConfig


class ProcurementConfig(AppConfig):
    """Procurement schedule and submittals (SUB-001). See procurement/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'procurement'

    def ready(self):
        import actions
        from core.registry import register_record_type

        from .actions import OrderByProvider, SubmittalProvider
        from .models import Submittal
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        # external_roles=(): the record index never shows submittals to externals. Subcontractors and
        # designers see theirs through procurement.access, whose rules are narrower than visibility.
        register_record_type(Submittal, prefix='SUB', transitions=TRANSITIONS, open_statuses=Submittal.OPEN_STATUSES,
                             draft_statuses=(Submittal.REQUIRED,), audit_fields=AUDIT_FIELDS, external_roles=())
        actions.register(SubmittalProvider())
        actions.register(OrderByProvider())

        from django.db.models.signals import post_save

        from contracts.models import MainContract

        from .models import recalculate_for_contract
        post_save.connect(recalculate_for_contract, sender=MainContract, dispatch_uid='procurement_contract_region')
