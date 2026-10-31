# Schema: one row per forward of a revision to a consultant (review finding M1).

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("procurement", "0001_initial"),
        ("tenancy", "0003_membership_area_teaminvite_area"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SubmittalForward",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "forwarded_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("note", models.TextField(blank=True)),
                ("comments", models.TextField(blank=True)),
                ("replied_at", models.DateTimeField(blank=True, null=True)),
                (
                    "consultant",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "forwarded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        editable=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="tenancy.organization",
                    ),
                ),
                (
                    "revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="forwards",
                        to="procurement.submittalrevision",
                    ),
                ),
            ],
            options={
                "ordering": ["forwarded_at", "pk"],
                "abstract": False,
                "base_manager_name": "all_objects",
            },
            managers=[
                ("objects", django.db.models.manager.Manager()),
                ("all_objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.AddField(
            model_name="submittalfile",
            name="forward",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="file_rows",
                to="procurement.submittalforward",
            ),
        ),
    ]
