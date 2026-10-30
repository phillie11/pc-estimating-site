# Runs INSIDE a one-off Siteline task on AWS (load-data.ps1 / load-data.sh pass it as `python -c`).
# Downloads the database export from the transfer bucket, loads it, resets the ID counters, prints counts.
#   argv: <bucket> <key>
import os
import sys

import boto3
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.apps import apps  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.management.color import no_style  # noqa: E402
from django.db import connection, transaction  # noqa: E402

bucket, key = sys.argv[1], sys.argv[2]
path = '/tmp/siteline-data.json.gz'
print(f'Downloading s3://{bucket}/{key}', flush=True)
boto3.client('s3').download_file(bucket, key, path)

# One transaction: if anything fails, nothing is left half-loaded
with transaction.atomic():
    call_command('loaddata', path, verbosity=1)
    # loaddata already resets PostgreSQL sequences; doing it again for every table is harmless and certain
    statements = connection.ops.sequence_reset_sql(no_style(), apps.get_models(include_auto_created=True))
    with connection.cursor() as cursor:
        for sql in statements:
            cursor.execute(sql)
print(f'Reset {len(statements)} ID sequences', flush=True)

os.remove(path)
print('--- row counts on AWS ---', flush=True)
call_command('model_counts', '--exclude', 'contenttypes', 'auth.permission', 'sessions', 'axes')
