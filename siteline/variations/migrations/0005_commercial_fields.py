"""Client variations onto the record base, step 3 of 3: the new commercial fields (extension of time, quotation due)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("variations", "0004_record_base_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="variation",
            name="eot_days_requested",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Extension of time requested (days)"),
        ),
        migrations.AddField(
            model_name="variation",
            name="eot_days_granted",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Extension of time granted (days)"),
        ),
        migrations.AddField(
            model_name="variation",
            name="quotation_due",
            field=models.DateField(blank=True, help_text="When the client needs our quotation by.", null=True,
                                   verbose_name="Quotation due"),
        ),
    ]
