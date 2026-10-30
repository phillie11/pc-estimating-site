from django.apps import AppConfig


class HolidaysConfig(AppConfig):
    """UK bank holidays by region and Construction Act day counting (holidays/calendar.py). No models."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'holidays'
