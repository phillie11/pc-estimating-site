"""
Client variations move onto core.models.RecordBase (docs/adr/0001-core-record-base.md, migration
plan step 1), step 1 of 3: columns.

raised_by is renamed to the shared created_by, so its data stays put. ref, visibility and the
other record-base columns are added; 0004 fills them, and 0005 adds the new commercial fields
(extension of time requested and granted, quotation due). Data and table changes are in separate
migrations so PostgreSQL never alters a table with row changes still pending in the same transaction.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0004_subcontractor_archive"),
        ("projects", "0011_project_archive"),
        ("rfis", "0005_remove_rfi_is_shared"),
        ("variations", "0002_alter_variation_project"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="variation",
            options={"base_manager_name": "all_objects", "default_manager_name": "objects", "ordering": ["-number"]},
        ),
        # Same column and constraint; the related name now comes from RecordBase ("variations" for Variation)
        migrations.AlterField(
            model_name="variation",
            name="project",
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name="%(class)ss",
                                    to="projects.project"),
        ),
        migrations.RenameField(model_name="variation", old_name="raised_by", new_name="created_by"),
        migrations.AlterField(
            model_name="variation",
            name="created_by",
            field=models.ForeignKey(
                editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="variation",
            name="ref",
            field=models.CharField(default="", editable=False, max_length=24),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="variation",
            name="visibility",
            field=models.CharField(
                choices=[("INTERNAL", "Internal only"), ("SHARED", "Shared with project collaborators")],
                default="INTERNAL", max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="variation",
            name="due_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="variation",
            name="ball_in_court_user",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                to=settings.AUTH_USER_MODEL, verbose_name="Ball in court",
            ),
        ),
        migrations.AddField(
            model_name="variation",
            name="ball_in_court_company",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                to="directory.subcontractor", verbose_name="Ball in court (company)",
            ),
        ),
        migrations.AddField(
            model_name="variation",
            name="updated_by",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="variation",
            name="deleted_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="variation",
            name="deleted_by",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="variation",
            name="retain_until",
            field=models.DateField(blank=True, help_text="Not purged before this date.", null=True),
        ),
        migrations.AddField(
            model_name="variation",
            name="legal_hold",
            field=models.BooleanField(default=False, help_text="Never purged while set."),
        ),
        migrations.AddField(
            model_name="variation",
            name="version",
            field=models.PositiveIntegerField(default=1, editable=False),
        ),
    ]
