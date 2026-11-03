"""
Make existing "issued a transmittal" activity entries staff only.

Until Release 3 these were shown to every external on the project when any recipient was external, so a client
could see the title of a transmittal sent only to a subcontractor (and a link they could not open). New entries
are staff only (documents.services.issue); this brings old ones into line. Reversing leaves them staff only: the
old visibility can't be recovered and should not be.
"""
from django.db import migrations


def hide(apps, schema_editor):
    ActivityEvent = apps.get_model('tenancy', 'ActivityEvent')
    # The model's default manager is tenant-scoped; the historical model in a migration is a plain manager.
    ActivityEvent._base_manager.filter(verb='issued', internal_only=False,
                                       url__contains='/transmittals/').update(internal_only=True)


class Migration(migrations.Migration):
    dependencies = [
        ('documents', '0001_initial'),
        ('tenancy', '0004_activityevent_activity_org_created'),
    ]
    operations = [migrations.RunPython(hide, migrations.RunPython.noop)]
