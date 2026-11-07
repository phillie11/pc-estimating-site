from django.apps import AppConfig


class CvrConfig(AppConfig):
    """Monthly CVR, cashflow forecast and dayworks, under the Commercial tab. See cvr/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cvr'
    verbose_name = 'CVR, cashflow and dayworks'

    def ready(self):
        import actions
        from actions.providers import HookProvider
        from core.registry import register_record_type

        from .models import CVRPeriod, DayworkSheet
        from .workflow import CVR_AUDIT, CVR_TRANSITIONS, DAYWORK_AUDIT, DAYWORK_TRANSITIONS
        # Both always internal. What waits on people comes from cvr/actions.py, not ball in court
        register_record_type(CVRPeriod, prefix='CVR', transitions=CVR_TRANSITIONS, open_statuses=(),
                             draft_statuses=(CVRPeriod.DRAFT,), audit_fields=CVR_AUDIT, external_roles=())
        register_record_type(DayworkSheet, prefix='DW', transitions=DAYWORK_TRANSITIONS, open_statuses=(),
                             draft_statuses=(DayworkSheet.DRAFT,), audit_fields=DAYWORK_AUDIT, external_roles=())
        actions.register(HookProvider('cvr', 'Monthly CVR', 'cvr.actions.cvr_items', staff=True,
                                      covers=('cvr.cvrperiod',), action='Prepare or sign off the CVR'))
        actions.register(HookProvider('daywork', 'Daywork sheets', 'cvr.actions.daywork_items', staff=True,
                                      covers=('cvr.dayworksheet',), action='Get it signed and valued'))
        # The client or consultant asked to sign a sheet in their portal (cvr/portal.py)
        actions.register(HookProvider('daywork_sign', 'Daywork sheets to sign', 'cvr.portal.client_items',
                                      staff=False, action='Check it and sign it'))
