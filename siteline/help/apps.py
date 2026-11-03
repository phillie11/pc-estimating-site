from django.apps import AppConfig


class HelpConfig(AppConfig):
    """Contextual help (help/content/*.md), global search and the first-run checklist for a new company."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'help'
    verbose_name = 'Help and getting started'
