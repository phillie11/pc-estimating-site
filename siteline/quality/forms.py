"""
Forms for Quality. Each asks only for what the record can't exist without and fills in the rest:
today, the signed-in person, this job, the company's usual permit length.
"""
import datetime

from django import forms
from django.utils import timezone

from core.forms import RecordFormMixin
from directory.models import Package
from projects.access import assignable_users
from projects.forms import StyledModelForm, style_widgets

from . import rules
from .models import ChecklistTemplate, Inspection, Penetration, Permit, QualitySettings
from .photos import validate_photo

MAX_CHECKS = 60


class StyledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)


class PhotoField(forms.FileField):
    """One photo, from the phone's camera straight away. Checked to be a real picture."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('required', False)
        kwargs.setdefault('widget', forms.ClearableFileInput(attrs={'accept': 'image/*', 'capture': 'environment'}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        f = super().clean(data, initial)
        if f:
            validate_photo(f)
        return f


def appointed_packages(project):
    return Package.objects.filter(project=project, status=Package.APPOINTED).select_related('subcontractor', 'trade')


class PackageChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f'{obj.subcontractor}{f" ({obj.trade})" if obj.trade_id else ""}'


# --- inspections -----------------------------------------------------------------------

class InspectionStartForm(StyledModelForm):
    class Meta:
        model = Inspection
        fields = ['template', 'area', 'package', 'ball_in_court_user', 'due_date']
        labels = {'ball_in_court_user': 'Who will inspect', 'due_date': 'Inspect by'}

    def __init__(self, *args, project=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields['template'].queryset = ChecklistTemplate.objects.filter(is_active=True)
        self.fields['template'].required = True
        self.fields['template'].empty_label = 'Choose a checklist'
        self.fields['template'].error_messages['required'] = 'Choose the checklist to inspect against.'
        self.fields['package'] = PackageChoice(appointed_packages(project), required=False, label='Whose work',
                                               empty_label='Our own work, or not one subcontractor',
                                               help_text='Snags raised from failed checks go to them.')
        self.fields['ball_in_court_user'].queryset = assignable_users(project)
        self.fields['ball_in_court_user'].required = True
        self.fields['ball_in_court_user'].empty_label = None
        self.fields['ball_in_court_user'].help_text = 'It goes on their Needs me until it is finished.'
        if not self.is_bound:
            self.initial.setdefault('ball_in_court_user', getattr(user, 'pk', None))
            self.initial.setdefault('due_date', timezone.localdate())


class ResultForm(forms.Form):
    """One check: pass, fail or N/A (the button pressed), with an optional note and photo."""
    result = forms.ChoiceField(choices=[(rules.PASS, 'Pass'), (rules.FAIL, 'Fail'), (rules.NA, 'N/A'), ('', 'Save')],
                               required=False)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}), max_length=2000)
    photo = PhotoField(label='Add a photo')


class FinishForm(forms.Form):
    notes = forms.CharField(label='General notes', required=False, widget=forms.Textarea(attrs={'rows': 3}))


class ReasonForm(forms.Form):
    reason = forms.CharField(label='Why', max_length=500, widget=forms.Textarea(attrs={'rows': 2}),
                             error_messages={'required': 'Say why, so it is in the record.'})


# --- checklist library -----------------------------------------------------------------

class TemplateForm(StyledModelForm):
    checks = forms.CharField(
        label='Checks, one per line', widget=forms.Textarea(attrs={'rows': 12}),
        help_text='Each line becomes one pass / fail / N/A check. Changing these never alters inspections '
                  'already started.', error_messages={'required': 'Type at least one check, one per line.'})

    class Meta:
        model = ChecklistTemplate
        fields = ['name', 'trade', 'description', 'checks', 'is_active']
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            self.initial['checks'] = '\n'.join(i.text for i in self.instance.items.all())
        if not self.instance.pk:
            del self.fields['is_active']

    def clean_checks(self):
        lines = [line.strip(' \t-•*') for line in self.cleaned_data['checks'].splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            raise forms.ValidationError('Type at least one check, one per line.')
        if len(lines) > MAX_CHECKS:
            raise forms.ValidationError(f'Up to {MAX_CHECKS} checks on one checklist. Split it into two.')
        long = [line for line in lines if len(line) > 300]
        if long:
            raise forms.ValidationError(f'Keep each check under 300 characters: "{long[0][:40]}…" is too long.')
        return lines


# --- permits ---------------------------------------------------------------------------

def _now_rounded():
    """Now, to the next quarter hour, for the default start."""
    now = timezone.localtime().replace(second=0, microsecond=0)
    return now + datetime.timedelta(minutes=(15 - now.minute % 15) % 15)


class PermitForm(RecordFormMixin, StyledModelForm):
    class Meta:
        model = Permit
        fields = ['permit_type', 'title', 'location', 'package', 'holder', 'starts_at', 'expires_at', 'method_notes']
        labels = {'title': 'What work is being done'}
        help_texts = {'title': 'e.g. Cut and weld steel bracket for the bar canopy.'}
        widgets = {'permit_type': forms.RadioSelect, 'method_notes': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, project=None, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['permit_type'].choices = rules.PERMIT_TYPE_CHOICES
        self.fields['permit_type'].error_messages['required'] = 'Choose the type of permit.'
        self.fields['title'].error_messages['required'] = 'Say what work the permit is for.'
        self.fields['location'].error_messages['required'] = 'Say where the work is.'
        self.fields['package'] = PackageChoice(appointed_packages(project), required=False,
                                               label='Subcontractor doing the work', empty_label='Our own people')
        if not self.instance.pk and not self.is_bound:
            start = _now_rounded()
            hours = QualitySettings.for_org(org).permit_hours if org is not None else 8
            self.initial.setdefault('starts_at', start)
            self.initial.setdefault('expires_at', start + datetime.timedelta(hours=hours))

    def clean(self):
        data = super().clean()
        kind, start, end = data.get('permit_type'), data.get('starts_at'), data.get('expires_at')
        if kind and start and end:
            problem = rules.validity_problem(kind, start, end)
            if problem:
                self.add_error('expires_at', problem)
            elif end <= timezone.now():
                self.add_error('expires_at', 'That time has already passed. Pick when the permit should run out.')
        return data


class ApproveForm(forms.Form):
    precautions = forms.MultipleChoiceField(label='Confirm each precaution is in place',
                                            widget=forms.CheckboxSelectMultiple, required=False)
    extra = forms.CharField(label='Extra precautions or conditions', required=False,
                            widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, permit=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.required = rules.precautions_for(permit.permit_type)
        self.fields['precautions'].choices = [(p, p) for p in self.required]

    def clean_precautions(self):
        ticked = self.cleaned_data['precautions']
        missing = [p for p in self.required if p not in ticked]
        if missing:
            raise forms.ValidationError(f'Tick every precaution once it is in place ({len(missing)} not ticked). '
                                        'If one can\'t be met, refuse the permit and say why.')
        return ticked


class CloseForm(forms.Form):
    safe = forms.BooleanField(label='The work is finished and the area has been left safe',
                              error_messages={'required': 'Confirm the area has been left safe before closing.'})
    fire_watch = forms.BooleanField(required=False, label='The fire watch has been done')
    note = forms.CharField(label='Note', required=False, max_length=500, widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, permit=None, minutes=60, **kwargs):
        super().__init__(*args, **kwargs)
        self.hot = permit is not None and permit.is_hot_works
        self.minutes = minutes
        if self.hot:
            self.fields['fire_watch'].label = f'The fire watch has been kept for {minutes} minutes after work stopped'
        else:
            del self.fields['fire_watch']

    def clean_fire_watch(self):
        if self.hot and not self.cleaned_data.get('fire_watch'):
            raise forms.ValidationError(f'Keep the fire watch for {self.minutes} minutes after hot works stop, '
                                        'then close the permit.')
        return self.cleaned_data.get('fire_watch')


# --- fire-stopping ---------------------------------------------------------------------

class PenetrationForm(RecordFormMixin, StyledModelForm):
    before_photo = PhotoField(label='Photo before sealing')
    after_photo = PhotoField(label='Photo of the finished seal')

    class Meta:
        model = Penetration
        fields = ['location', 'element', 'fire_rating', 'service', 'size', 'system', 'detail_reference', 'package',
                  'installer', 'installed_on', 'notes']
        widgets = {'notes': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['location'].error_messages['required'] = 'Say where it is: level, room and wall.'
        self.fields['fire_rating'].choices = [('', 'Not known yet')] + Penetration.RATING_CHOICES
        self.fields['service'].choices = [('', 'Choose…')] + Penetration.SERVICE_CHOICES
        self.fields['package'] = PackageChoice(appointed_packages(project), required=False,
                                               label='Installed by (subcontractor)', empty_label='Not one of our subs')
        self.fields['installed_on'].help_text = 'Leave blank until it is sealed; it fills in today when you mark it sealed.'
        if self.instance.pk:
            del self.fields['before_photo']
            del self.fields['after_photo']
        else:
            self.fields['after_photo'].help_text = 'Adding this marks it as sealed, ready to inspect.'

    def clean_installed_on(self):
        d = self.cleaned_data.get('installed_on')
        if d and d > timezone.localdate():
            raise forms.ValidationError('That date is in the future. Enter the day it was sealed.')
        return d


class PenetrationPhotoForm(forms.Form):
    stage = forms.ChoiceField(label='Is this', choices=[('BEFORE', 'Before sealing'), ('AFTER', 'The finished seal')],
                              widget=forms.RadioSelect, initial='AFTER')
    photo = PhotoField(label='Photo', required=True, error_messages={'required': 'Take or choose a photo to add.'})


class NoteForm(forms.Form):
    note = forms.CharField(label='Note', required=False, max_length=1000, widget=forms.Textarea(attrs={'rows': 2}))


class SettingsForm(StyledModelForm):
    class Meta:
        model = QualitySettings
        fields = ['permit_hours', 'expiry_warning_hours', 'fire_watch_minutes']
