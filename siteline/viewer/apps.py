from django.apps import AppConfig


class ViewerConfig(AppConfig):
    """The in-browser drawing viewer (pdf.js) and snag/RFI pins on drawings. See viewer/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'viewer'
    verbose_name = 'Drawing viewer'
