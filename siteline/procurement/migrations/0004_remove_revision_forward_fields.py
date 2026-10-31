# Schema: drop the single-forward columns from SubmittalRevision, now that 0003 moved them into
# SubmittalForward rows. Reversing adds them back (empty) before 0003's reverse refills them.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("procurement", "0003_forwards_data"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="submittalrevision",
            name="consultant_comments",
        ),
        migrations.RemoveField(
            model_name="submittalrevision",
            name="consultant_replied_at",
        ),
        migrations.RemoveField(
            model_name="submittalrevision",
            name="forward_note",
        ),
        migrations.RemoveField(
            model_name="submittalrevision",
            name="forwarded_at",
        ),
        migrations.RemoveField(
            model_name="submittalrevision",
            name="forwarded_by",
        ),
        migrations.RemoveField(
            model_name="submittalrevision",
            name="forwarded_to",
        ),
    ]
