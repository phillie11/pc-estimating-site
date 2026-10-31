from django.apps import AppConfig


class DiaryConfig(AppConfig):
    """Site diary, site photos and the phone-first "Today on site" page. See diary/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'diary'
    verbose_name = 'Site diary'

    def ready(self):
        import actions
        from actions.providers import HookProvider
        from core.registry import register_record_type

        from .models import DiaryEntry
        from .workflow import AUDIT_FIELDS, TRANSITIONS
        # Always internal (external_roles=()); nothing waits on a ball-in-court person (no open statuses)
        register_record_type(DiaryEntry, prefix='DY', transitions=TRANSITIONS, open_statuses=(),
                             audit_fields=AUDIT_FIELDS, external_roles=())
        # "Needs me": a project manager whose live job has no diary entry for a working day that is now due
        actions.register(HookProvider('diary_missing', 'Site diary to write up', 'diary.actions.items', staff=True,
                                      covers=('diary.diaryentry',), action='Write up the site diary for that day'))
