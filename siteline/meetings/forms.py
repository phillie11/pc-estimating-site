from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.forms import RecordFormMixin
from projects.access import assignable_users, staff_users
from projects.forms import StyledModelForm, style_widgets

from .models import (INTERNAL_PREFIX, KIND_CHOICES, PROGRESS, AgendaTemplate, Meeting, MeetingAction,
                     MeetingSettings, parse_agenda_line)


class PeopleField(forms.ModelMultipleChoiceField):
    widget = forms.CheckboxSelectMultiple

    def __init__(self, groups, **kwargs):
        ids = [u.pk for _, rows in groups for u, _ in rows]
        super().__init__(queryset=get_user_model().objects.filter(pk__in=ids), required=False, **kwargs)
        self.labels = {u.pk: f'{u.full_name} · {note}' for _, rows in groups for u, note in rows}

    def label_from_instance(self, obj):
        return self.labels.get(obj.pk, obj.full_name)


class MeetingForm(forms.Form):
    """New meeting: only the kind and the date are needed; the agenda comes from the company's template."""
    kind = forms.ChoiceField(label='Kind of meeting', choices=KIND_CHOICES, initial=PROGRESS, widget=forms.RadioSelect)
    held_on = forms.DateField(label='Date')
    starts_at = forms.RegexField(label='Time', required=False, regex=r'^([01]\d|2[0-3]):[0-5]\d$',
                                 error_messages={'invalid': 'Enter a time like 09:30.'},
                                 widget=forms.TimeInput(attrs={'type': 'time'}))
    location = forms.CharField(label='Where', required=False, max_length=200,
                               help_text="Site office, Teams, the client's office.")
    attendees = forms.Field(required=False)  # replaced in __init__
    others = forms.CharField(label='Others (no login)', required=False, widget=forms.Textarea(attrs={'rows': 3}),
                             help_text='One per line: name, company. They are recorded in the minutes but not emailed.')

    def __init__(self, *args, groups=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['attendees'] = PeopleField(groups, label='Who is invited',
                                               help_text='Everyone ticked with a login gets the minutes by email.')
        self.fields['held_on'].initial = timezone.localdate()
        style_widgets(self)


class AttendeesForm(forms.Form):
    attendees = forms.Field(required=False)  # replaced in __init__
    others = MeetingForm.base_fields['others']

    def __init__(self, *args, groups=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['attendees'] = PeopleField(groups, label='Who is invited',
                                               help_text='Everyone ticked with a login gets the minutes by email.')
        style_widgets(self)


class MeetingDetailsForm(RecordFormMixin, StyledModelForm):
    class Meta:
        model = Meeting
        fields = ['title', 'held_on', 'starts_at', 'location', 'chair']
        labels = {'title': 'Title'}
        widgets = {'starts_at': forms.TimeInput(attrs={'type': 'time'})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['chair'].queryset = staff_users(project.organization, 'delivery')
        self.fields['chair'].empty_label = 'Nobody named'


class ActionForm(StyledModelForm):
    class Meta:
        model = MeetingAction
        fields = ['title', 'owner', 'owner_name', 'due_date', 'item']
        labels = {'title': 'Action'}
        widgets = {'title': forms.Textarea(attrs={'rows': 2, 'maxlength': 200})}

    def __init__(self, *args, meeting=None, staff_ids=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.staff_ids = staff_ids
        self.fields['owner'].queryset = assignable_users(meeting.project).exclude(
            project_memberships__project=meeting.project, project_memberships__role='BIDDER')
        self.fields['owner'].empty_label = 'Choose someone, or type a name below'
        self.fields['item'].queryset = meeting.items.all()
        self.fields['item'].empty_label = 'General'
        self.fields['item'].label_from_instance = lambda i: f'{i.heading} (internal only)' if i.internal_only else i.heading
        self.fields['title'].help_text = 'What needs doing, in a sentence.'
        self.fields['title'].error_messages['required'] = 'Say what needs doing.'

    def clean(self):
        data = super().clean()
        if data.get('owner') and data.get('owner_name'):
            data['owner_name'] = ''  # a login wins: they get it on Needs me
        item, owner = data.get('item'), data.get('owner')
        if item is not None and item.internal_only and owner is not None and self.staff_ids is not None \
                and owner.pk not in self.staff_ids:
            self.add_error('owner', f'"{item.heading}" is internal only, so its actions stay with our team. Give it to '
                                    'someone in our team, or put it under a heading that is shared.')
        return data


class ActionUpdateForm(forms.Form):
    status = forms.ChoiceField(choices=MeetingAction.STATUS_CHOICES, label='Status')
    update = forms.CharField(label='Latest update', required=False, max_length=2000,
                             widget=forms.Textarea(attrs={'rows': 2}))


class AgendaTemplateForm(StyledModelForm):
    class Meta:
        model = AgendaTemplate
        fields = ['name', 'items']
        widgets = {'items': forms.Textarea(attrs={'rows': 12})}
        help_texts = {'items': 'One item per line, in order. Start a line with "Internal:" to keep that heading, its '
                               'minutes and its actions from clients, consultants and subcontractors, e.g. '
                               '"Internal: Commercial and costs".'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['items'].error_messages['required'] = 'Add at least one agenda item, one per line.'
        self.fields['name'].error_messages['required'] = 'Give the agenda a name, e.g. "Site progress meeting".'

    def clean_items(self):
        rows = [parse_agenda_line(ln) for ln in self.cleaned_data['items'].splitlines() if ln.strip()]
        if any(not heading for heading, _ in rows):
            raise forms.ValidationError('Put the heading after "Internal:", e.g. "Internal: Commercial and costs".')
        if not rows:
            raise forms.ValidationError('Add at least one agenda item, one per line.')
        if len(rows) > 40:
            raise forms.ValidationError('Keep it to 40 items or fewer.')
        return '\n'.join(f'{INTERNAL_PREFIX} {h[:200]}' if internal else h[:200] for h, internal in rows)


class MeetingSettingsForm(StyledModelForm):
    class Meta:
        model = MeetingSettings
        fields = ['minutes_due_days']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['minutes_due_days'].error_messages.update({
            'min_value': 'Enter 0 (the same day) or more.', 'max_value': 'Enter 14 days or fewer.'})
