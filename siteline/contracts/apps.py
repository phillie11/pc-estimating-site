from django.apps import AppConfig


class ContractsConfig(AppConfig):
    """Main contract set-up per project (internal): form, key terms, payment terms, vocabulary."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'contracts'
