from django.apps import AppConfig


class CoreConfig(AppConfig):
    """The shared record base: numbering, the record index, links, audit and workflow. See docs/adr/0001."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'
