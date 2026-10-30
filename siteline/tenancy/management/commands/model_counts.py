"""
Print the number of rows in every table, across every company.

Used to check a database move (PythonAnywhere MySQL -> AWS PostgreSQL): run it on
both sides, save the output, and compare the two files. See infra/scripts/migrate-data.md.

    python manage.py model_counts > counts-mysql.txt
    python manage.py model_counts --exclude contenttypes auth.permission sessions admin.logentry

Counts use each model's unscoped manager (all_objects), never the tenant-scoped
`objects`, so every organisation's rows are included.
"""
from django.apps import apps
from django.core.management.base import BaseCommand


def unscoped_manager(model):
    # TenantModel.all_objects is unscoped (and is also the base manager); other models have no scoping
    return getattr(model, 'all_objects', None) or model._base_manager


class Command(BaseCommand):
    help = 'Print row counts per model (all organisations), one "app_label.Model count" line each.'

    def add_arguments(self, parser):
        parser.add_argument('--exclude', nargs='*', default=[],
                            help='app labels or app_label.Model names to leave out')

    def handle(self, *args, exclude=(), **opts):
        skip = {e.lower() for e in exclude}
        total = 0
        for model in sorted(apps.get_models(include_auto_created=True), key=lambda m: m._meta.label_lower):
            label = model._meta.label_lower
            if model._meta.app_label in skip or label in skip or not model._meta.managed:
                continue
            count = unscoped_manager(model).count()
            total += count
            self.stdout.write(f'{model._meta.label} {count}')
        self.stdout.write(f'TOTAL {total}')
