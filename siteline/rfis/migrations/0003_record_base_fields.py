"""
RFIs move onto core.models.RecordBase (docs/adr/0001-core-record-base.md), step 1 of 3: columns.

subject, assigned_to and raised_by are renamed to the shared names (title, ball_in_court_user,
created_by), so their data stays put. visibility is added beside is_shared; 0004 copies it across
and fills ref, and 0005 drops is_shared. Data and table changes are in separate migrations so
PostgreSQL never alters a table with row changes still pending in the same transaction.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0004_subcontractor_archive"),
        ("projects", "0011_project_archive"),
        ("rfis", "0002_alter_rfi_project"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="rfi",
            options={"base_manager_name": "all_objects", "default_manager_name": "objects", "ordering": ["-number"],
                     "verbose_name": "RFI"},
        ),
        # Same column and constraint; the related name now comes from RecordBase ("rfis" for RFI)
        migrations.AlterField(
            model_name="rfi",
            name="project",
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name="%(class)ss",
                                    to="projects.project"),
        ),
        migrations.RenameField(model_name="rfi", old_name="subject", new_name="title"),
        migrations.RenameField(model_name="rfi", old_name="assigned_to", new_name="ball_in_court_user"),
        migrations.RenameField(model_name="rfi", old_name="raised_by", new_name="created_by"),
        migrations.AlterField(
            model_name="rfi",
            name="ball_in_court_user",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                to=settings.AUTH_USER_MODEL, verbose_name="Ball in court",
            ),
        ),
        migrations.AlterField(
            model_name="rfi",
            name="created_by",
            field=models.ForeignKey(
                editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="rfi",
            name="ref",
            field=models.CharField(default="", editable=False, max_length=24),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="rfi",
            name="visibility",
            field=models.CharField(
                choices=[("INTERNAL", "Internal only"), ("SHARED", "Shared with project collaborators")],
                default="SHARED", max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="rfi",
            name="ball_in_court_company",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                to="directory.subcontractor", verbose_name="Ball in court (company)",
            ),
        ),
        migrations.AddField(
            model_name="rfi",
            name="updated_by",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="rfi",
            name="deleted_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="rfi",
            name="deleted_by",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="rfi",
            name="retain_until",
            field=models.DateField(blank=True, help_text="Not purged before this date.", null=True),
        ),
        migrations.AddField(
            model_name="rfi",
            name="legal_hold",
            field=models.BooleanField(default=False, help_text="Never purged while set."),
        ),
        migrations.AddField(
            model_name="rfi",
            name="version",
            field=models.PositiveIntegerField(default=1, editable=False),
        ),
    ]
