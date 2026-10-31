from django.apps import AppConfig


class CostsConfig(AppConfig):
    """Cost control: cost codes, budgets, commitments, actuals and the cost ledger (costs/ledger.py)."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'costs'
    verbose_name = 'Cost control'

    def ready(self):
        from actions import registry
        from actions.providers import HookProvider
        # Jobs whose forecast margin is under the company threshold, for the job's PM and admins.
        # Registered here rather than in actions.providers.HOOKS so this app stays self-contained.
        registry.register(HookProvider('cost_margin', 'Jobs below margin', 'costs.actions.items', staff=True,
                                       action='Review the cost report'))
