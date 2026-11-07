from django.apps import AppConfig


class ApiConfig(AppConfig):
    """The read-only company API (/api/v1/) and Settings → API. See api/views.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'
    verbose_name = 'Read-only API'
