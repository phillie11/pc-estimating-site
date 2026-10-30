"""
"Needs me": everything waiting on a person, from every app, in one list.

    import actions
    actions.register(provider)          # from your AppConfig.ready()
    actions.for_user(request)           # [Item], soonest due first
    actions.grouped(items)              # Overdue / Today / This week / Later

See actions/registry.py for the provider contract and actions/providers.py for the built-ins.
"""
from .registry import (GROUPS, Item, count_for_user, for_user, grouped, kinds, providers, register,  # noqa: F401
                       request_for, total, unregister)
