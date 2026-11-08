from django.apps import AppConfig


class AIConfig(AppConfig):
    """AI drafting: company switch, budget, usage, playbooks, and the tender reader's bid brief. See models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai'
    verbose_name = 'AI drafting'

    def ready(self):
        from . import checks, tasks  # noqa: F401 - the ANTHROPIC_LOG check, and the worker task ai.run
