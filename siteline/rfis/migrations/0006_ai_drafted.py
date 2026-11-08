"""
AI-drafted RFIs: the ai_drafted and ai_basis columns, and no register number while an RFI is a draft (it takes
one when issued).

Going back: before the columns go, every draft is made internal (the code before this hides nothing by status,
so a shared draft would reach the client) and every draft without a number is given the next free one on its
project, since number can't be empty there.
"""
from django.db import migrations, models


def before_going_back(apps, schema_editor):
    RFI = apps.get_model('rfis', 'RFI')
    drafts = RFI._base_manager.filter(status='DRAFT')
    drafts.update(visibility='INTERNAL')
    highest = {}
    for rfi in drafts.filter(number__isnull=True).order_by('project_id', 'pk'):
        if rfi.project_id not in highest:
            top = RFI._base_manager.filter(project_id=rfi.project_id).aggregate(m=models.Max('number'))['m']
            highest[rfi.project_id] = top or 0
        highest[rfi.project_id] += 1
        RFI._base_manager.filter(pk=rfi.pk).update(number=highest[rfi.project_id],
                                                   ref=f'RFI-{highest[rfi.project_id]:03d}')


class Migration(migrations.Migration):

    dependencies = [
        ("rfis", "0005_remove_rfi_is_shared"),
    ]

    operations = [
        migrations.AddField(
            model_name="rfi",
            name="ai_basis",
            field=models.TextField(
                blank=True,
                editable=False,
                verbose_name="Why it matters (AI draft, staff only)",
            ),
        ),
        migrations.AddField(
            model_name="rfi",
            name="ai_drafted",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AlterField(
            model_name="rfi",
            name="number",
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        # Last, so that on the way back it runs first, while the columns are still there
        migrations.RunPython(migrations.RunPython.noop, before_going_back),
    ]
