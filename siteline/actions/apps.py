from django.apps import AppConfig


class ActionsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'actions'
    verbose_name = 'Needs me'

    def ready(self):
        from . import registry
        from .providers import BUILT_IN
        for provider in BUILT_IN:
            registry.register(provider())
