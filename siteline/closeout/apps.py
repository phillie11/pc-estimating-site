from django.apps import AppConfig


class CloseoutConfig(AppConfig):
    """Close-out: practical completion, defects period, O&M manual, golden thread handover. See closeout/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'closeout'
    verbose_name = 'Close-out'

    def ready(self):
        from django.db.models.signals import post_save, pre_save

        import actions
        from contracts.models import MainContract
        from projects.models import Issue

        from . import signals
        from .actions import DefectsEndProvider, DefectToAssignProvider
        actions.register(DefectsEndProvider())
        actions.register(DefectToAssignProvider())
        pre_save.connect(signals.defect_after_pc, sender=Issue, dispatch_uid='closeout_defect_after_pc')
        pre_save.connect(signals.remember_status, sender=Issue, dispatch_uid='closeout_remember_status')
        post_save.connect(signals.tell_reporter, sender=Issue, dispatch_uid='closeout_tell_reporter')
        post_save.connect(signals.contract_pc, sender=MainContract, dispatch_uid='closeout_contract_pc')
