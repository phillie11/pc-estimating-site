from django.apps import AppConfig


class HandoverPackConfig(AppConfig):
    """The pricing workbook on a bid and the internal estimating-to-delivery handover pack. See models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'handoverpack'
    verbose_name = 'Pricing workbook and handover pack'

    def ready(self):
        import actions
        from actions import providers
        from actions.providers import HookProvider
        # Its figures ride on the shared my_jobs query, so the badge costs no extra query (actions.py)
        if 'handoverpack.actions.job_annotations' not in providers.JOB_ANNOTATIONS:
            providers.JOB_ANNOTATIONS.append('handoverpack.actions.job_annotations')
        # Open items given to this person, on packs they can open (handoverpack/actions.py). Staff only
        actions.register(HookProvider('handover_item', 'Handover open items', 'handoverpack.actions.items',
                                      staff=True, action='Sort it out and mark it done'))
