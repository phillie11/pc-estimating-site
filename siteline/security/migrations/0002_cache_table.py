"""
The database cache table (settings.CACHES, used by the rate limits). Django makes it with
`manage.py createcachetable` rather than a migration; running that here means every deploy that
already runs `migrate` gets it, with nothing extra to remember. createcachetable skips a table
that already exists, so running it by hand as well does no harm.
"""
from django.conf import settings
from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    call_command('createcachetable', database=schema_editor.connection.alias, verbosity=0)


def drop_cache_table(apps, schema_editor):
    for cache in settings.CACHES.values():
        if cache.get('BACKEND', '').endswith('DatabaseCache'):
            schema_editor.execute(f'DROP TABLE IF EXISTS {schema_editor.quote_name(cache["LOCATION"])}')


class Migration(migrations.Migration):
    dependencies = [('security', '0001_two_factor')]

    operations = [migrations.RunPython(create_cache_table, drop_cache_table)]
