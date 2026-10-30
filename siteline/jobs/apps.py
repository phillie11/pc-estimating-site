from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'jobs'

    def ready(self):
        # Register every app's tasks (a `tasks.py` module) so enqueue() can check names and the
        # worker can run them. The email outbox's task lives in jobs/outbox.py.
        from django.utils.module_loading import autodiscover_modules

        from . import checks, outbox  # noqa: F401
        autodiscover_modules('tasks')
