"""RFIs onto the record base, step 3 of 3: is_shared has been copied into visibility (0004), so it goes."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("rfis", "0004_record_base_data"),
    ]

    operations = [
        migrations.RemoveField(model_name="rfi", name="is_shared"),
    ]
