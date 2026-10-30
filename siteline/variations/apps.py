from django.apps import AppConfig


class VariationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "variations"

    def ready(self):
        from core.registry import register_record_type

        from .models import Variation
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        # Submitted: waiting on the client's decision ("Needs me" for them)
        register_record_type(Variation, prefix='VO', transitions=TRANSITIONS,
                             open_statuses=(Variation.STATUS_SUBMITTED,), draft_statuses=(Variation.STATUS_DRAFT,),
                             audit_fields=AUDIT_FIELDS,
                             # Clients and consultants only, as projects.access.can_see_variations: never subcontractors
                             external_roles=('CLIENT', 'CONSULTANT'))
