from django.apps import AppConfig


class MeetingsConfig(AppConfig):
    """Site progress, client and pre-start meetings: agendas, minutes and actions. See meetings/models.py."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'meetings'
    verbose_name = 'Meetings'

    def ready(self):
        import actions
        from actions.providers import HookProvider, register_record_formatter, worded
        from core.registry import register_record_type

        from .models import Meeting, MeetingAction
        from .workflow import ACTION_AUDIT_FIELDS, ACTION_TRANSITIONS, AUDIT_FIELDS, TRANSITIONS
        # Both always internal in the index (external_roles=()): externals reach issued minutes through
        # attendance, and their own actions through the hook below. Staff "Needs me" comes from the index:
        # the chair until the minutes are issued, then each action's owner until it's done.
        register_record_type(Meeting, prefix='MTG', transitions=TRANSITIONS, open_statuses=(Meeting.STATUS_PLANNED,),
                             draft_statuses=(Meeting.STATUS_PLANNED,), audit_fields=AUDIT_FIELDS, external_roles=())
        register_record_type(MeetingAction, prefix='ACT', transitions=ACTION_TRANSITIONS,
                             open_statuses=(MeetingAction.OPEN,), draft_statuses=(MeetingAction.OPEN,),
                             audit_fields=ACTION_AUDIT_FIELDS, external_roles=())
        # How staff Needs me words them (the record index rows; actions.providers RECORD_FORMATTERS)
        register_record_formatter('meetings.meeting', worded(Meeting.NEEDS_ME_ACTION, Meeting.NEEDS_ME_LABEL))
        register_record_formatter('meetings.meetingaction',
                                  worded(MeetingAction.NEEDS_ME_ACTION, MeetingAction.NEEDS_ME_LABEL))
        # "Needs me" for people outside the company: their actions from issued minutes (staff=False: staff get
        # theirs from the record index, so the badge on every staff page costs no extra query)
        actions.register(HookProvider('meeting_action', 'Meeting actions', 'meetings.actions.items', staff=False,
                                      action=MeetingAction.NEEDS_ME_ACTION))
