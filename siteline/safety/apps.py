from django.apps import AppConfig


class SafetyConfig(AppConfig):
    """Incidents and near misses, the CDM register and QR site sign-in. See safety/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'safety'
    verbose_name = 'Safety'

    def ready(self):
        import actions
        from core.registry import register_record_type

        from .models import Incident
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        # Always internal (external_roles=()). Open incidents wait on their ball-in-court person (the
        # project manager), so they reach Needs me through the record index.
        register_record_type(Incident, prefix='INC', transitions=TRANSITIONS,
                             open_statuses=Incident.OPEN_STATUSES, audit_fields=AUDIT_FIELDS, external_roles=())
        # "Needs me": incident actions, RIDDOR reports due, CDM gaps before the start on site (one query)
        from .actions import SafetyProvider
        actions.register(SafetyProvider())
