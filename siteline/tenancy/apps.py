from django.apps import AppConfig


class TenancyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tenancy"

    def ready(self):
        # A write in the request forgets everything the per-request memo held (tenancy.context)
        from django.db.models.signals import m2m_changed, post_delete, post_save

        from .context import clear_request_memo
        for signal in (post_save, post_delete, m2m_changed):
            signal.connect(clear_request_memo, dispatch_uid=f'tenancy-request-memo-{signal}')
