from django.apps import AppConfig


class HandoverPackConfig(AppConfig):
    """The pricing workbook on a bid and the internal estimating-to-delivery handover pack. See models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'handoverpack'
    verbose_name = 'Pricing workbook and handover pack'

    def ready(self):
        import actions
        from actions.providers import HookProvider
        # Open items given to this person, on packs they can open (handoverpack/actions.py). Staff only
        actions.register(HookProvider('handover_item', 'Handover open items', 'handoverpack.actions.items',
                                      staff=True, action='Sort it out and mark it done'))
