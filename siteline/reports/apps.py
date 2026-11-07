from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """Company reports for owners and admins (no models: every figure is read from its own module)."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'reports'
    verbose_name = 'Company reports'
