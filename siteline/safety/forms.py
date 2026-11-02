import datetime

from django import forms
from django.utils import timezone

from core.forms import RecordFormMixin
from diary.forms import phone_friendly
from diary.photos import validate_photo
from projects.files import validate_upload
from projects.forms import StyledModelForm

from . import rules
from .models import CDMProfile, DutyHolder, Incident, IncidentAction, PhasePlan, SiteAttendance

DATETIME = forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')
DATE = forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')
CAMERA = {'accept': 'image/*', 'capture': 'environment'}
YES_NO = [(False, 'No'), (True, 'Yes')]


def _yes_no(value):
    return value in (True, 'True', 'true', '1', 'on')


class ReportForm(StyledModelForm):
    """The phone form: what, where, when (now), a photo, was anyone hurt. Everything else comes later."""
    photo = forms.FileField(label='Photo', required=False, widget=forms.ClearableFileInput(attrs=CAMERA),
                            help_text='Optional. Take one now or add more later.')

    class Meta:
        model = Incident
        fields = ['description', 'location', 'occurred_at', 'anyone_hurt']
        widgets = {'occurred_at': DATETIME,
                   'anyone_hurt': forms.RadioSelect(choices=YES_NO),
                   'description': forms.Textarea(attrs={'rows': 3, 'placeholder': 'e.g. Board fell off the trolley '
                                                                                  'by the goods lift. Nobody hurt.'})}
        labels = {'description': 'What happened?', 'location': 'Where?', 'occurred_at': 'When?'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        phone_friendly(self)
        self.fields['description'].widget.attrs['rows'] = 3
        self.fields['anyone_hurt'] = forms.TypedChoiceField(
            label='Was anyone hurt?', choices=YES_NO, coerce=_yes_no, widget=forms.RadioSelect, initial=False,
            required=False, empty_value=False)
        when = self.fields['occurred_at']
        when.initial = timezone.localtime().replace(second=0, microsecond=0)
        when.required, when.show_hidden_initial = False, False  # blank means now
        when.help_text = 'Leave as now if it just happened.'
        self.fields['location'].help_text = ''
        self.fields['location'].widget.attrs['placeholder'] = 'e.g. First floor kitchen'

    def clean_occurred_at(self):
        when = self.cleaned_data.get('occurred_at') or timezone.now()
        if when and when > timezone.now() + datetime.timedelta(minutes=5):
            raise forms.ValidationError('That time is in the future. Put when it happened.')
        return when

    def clean_photo(self):
        f = self.cleaned_data.get('photo')
        if f:
            validate_photo(f)
        return f


class IncidentForm(RecordFormMixin, StyledModelForm):
    """Everything about an incident after the first report: the person hurt, RIDDOR and the investigation."""

    class Meta:
        model = Incident
        fields = ['kind', 'description', 'location', 'occurred_at', 'anyone_hurt',
                  'injured_name', 'injured_company', 'injured_is_worker', 'injury_details', 'first_aid_given',
                  'taken_to_hospital', 'days_off',
                  'riddor_category', 'riddor_reported_on', 'riddor_reference',
                  'immediate_action', 'root_cause', 'findings']
        widgets = {'occurred_at': DATETIME, 'riddor_reported_on': DATE}
        labels = {'description': 'What happened', 'riddor_category': 'Does it need reporting under RIDDOR?'}

    SECTIONS = (
        ('What happened', ('kind', 'description', 'location', 'occurred_at', 'anyone_hurt')),
        ('The person hurt', ('injured_name', 'injured_company', 'injured_is_worker', 'injury_details',
                             'first_aid_given', 'taken_to_hospital', 'days_off')),
        ('RIDDOR', ('riddor_category', 'riddor_reported_on', 'riddor_reference')),
        ('Investigation', ('immediate_action', 'root_cause', 'findings')),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        phone_friendly(self)
        self.fields['riddor_category'].help_text = rules.ADVISORY

    def sections(self):
        return [(title, [self[name] for name in names]) for title, names in self.SECTIONS]

    def clean(self):
        data = super().clean()
        if data.get('riddor_reported_on') and not rules.is_reportable(data.get('riddor_category')):
            self.add_error('riddor_reported_on', 'Pick the RIDDOR category it was reported under, or clear this date.')
        when = data.get('riddor_reported_on')
        if when and when > timezone.localdate():
            self.add_error('riddor_reported_on', 'That date is in the future.')
        return data


class ActionForm(StyledModelForm):
    class Meta:
        model = IncidentAction
        fields = ['description', 'owner', 'due_date']
        widgets = {'due_date': DATE}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        phone_friendly(self)
        # Staff on the job only: an action's owner must be able to open the incident (externals never can)
        from .access import job_staff
        self.fields['owner'].queryset = job_staff(project)
        self.fields['owner'].label_from_instance = lambda u: u.full_name or u.email
        self.fields['owner'].empty_label = 'Nobody yet'


class PhotoForm(forms.Form):
    photo = forms.FileField(label='Photo', widget=forms.ClearableFileInput(attrs=CAMERA))
    caption = forms.CharField(max_length=200, required=False)

    def clean_photo(self):
        f = self.cleaned_data['photo']
        validate_photo(f)
        return f


class CDMForm(StyledModelForm):
    NOTIFICATION = ('notifiable', 'working_days', 'peak_workers', 'person_days', 'f10_reference', 'f10_submitted_on')
    DATES = ('pci_received_on', 'cpp_in_place_on', 'hs_file_due_on', 'hs_file_issued_on', 'notes')

    def notification_fields(self):
        return [self[n] for n in self.NOTIFICATION]

    def date_fields(self):
        return [self[n] for n in self.DATES]

    class Meta:
        model = CDMProfile
        fields = ['notifiable', 'working_days', 'peak_workers', 'person_days', 'f10_reference', 'f10_submitted_on',
                  'pci_received_on', 'cpp_in_place_on', 'hs_file_due_on', 'hs_file_issued_on', 'notes']
        widgets = {f: DATE for f in ('f10_submitted_on', 'pci_received_on', 'cpp_in_place_on', 'hs_file_due_on',
                                     'hs_file_issued_on')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        phone_friendly(self)

    def clean(self):
        data = super().clean()
        if (data.get('f10_reference') or data.get('f10_submitted_on')) and data.get('notifiable') == 'NO':
            self.add_error('notifiable', 'An F10 has been recorded, so set this to "Yes, notifiable".')
        return data


class DutyHolderForm(StyledModelForm):
    class Meta:
        model = DutyHolder
        fields = ['role', 'company', 'contact_name', 'email', 'phone', 'appointed_on', 'notes']
        widgets = {'appointed_on': DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        phone_friendly(self)
        self.fields['phone'].widget.attrs['inputmode'] = 'tel'


class PhasePlanForm(forms.ModelForm):
    class Meta:
        model = PhasePlan
        fields = ['file', 'note']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['file'].label = 'Construction phase plan'
        self.fields['file'].widget.attrs['accept'] = '.pdf,.doc,.docx'
        self.fields['note'].widget.attrs['placeholder'] = 'e.g. Updated for the second floor works'

    def clean_file(self):
        f = self.cleaned_data['file']
        validate_upload(f)
        return f


class VisitorForm(forms.Form):
    """The public sign-in page: no account, just a name and company."""
    name = forms.CharField(label='Your name', max_length=150,
                           error_messages={'required': 'Type your name so the site team knows who is on site.'},
                           widget=forms.TextInput(attrs={'autocomplete': 'name', 'autocapitalize': 'words'}))
    company = forms.CharField(label='Your company', max_length=150, required=False,
                              widget=forms.TextInput(attrs={'autocomplete': 'organization'}),
                              help_text='Leave blank if you are visiting on your own account.')

    def _clean_text(self, name):
        value = ' '.join(self.cleaned_data.get(name, '').split())  # no line breaks or control characters
        return ''.join(ch for ch in value if ch.isprintable())

    def clean_name(self):
        value = self._clean_text('name')
        if not value:
            raise forms.ValidationError('Type your name so the site team knows who is on site.')
        return value

    def clean_company(self):
        return self._clean_text('company')


class ManualSignInForm(StyledModelForm):
    """The site team signing someone in who has no phone with them."""

    class Meta:
        model = SiteAttendance
        fields = ['name', 'company', 'kind']
        labels = {'kind': 'They are'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        phone_friendly(self)
        self.fields['kind'].initial = SiteAttendance.KIND_VISITOR
