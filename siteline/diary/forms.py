from django import forms
from django.utils import timezone

from changes.models import ChangeEvent
from core.forms import RecordFormMixin
from directory.models import Package
from projects.forms import StyledModelForm
from rfis.models import RFI

from .models import (DelayLine, DeliveryLine, DiaryEntry, DiarySettings, InstructionLine, LabourLine, PlantLine,
                     SitePhoto, VisitorLine)
from .photos import MAX_FILES, validate_photo

TIME = forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')


def phone_friendly(form):
    """Big inputs, the right keyboard on a phone, and nothing that zooms the page."""
    for name, field in form.fields.items():
        w = field.widget
        if isinstance(field, (forms.IntegerField, forms.DecimalField)):
            w.attrs.setdefault('inputmode', 'decimal' if isinstance(field, forms.DecimalField) else 'numeric')
        if isinstance(w, forms.Textarea):
            w.attrs['rows'] = 3


class EntryForm(RecordFormMixin, StyledModelForm):
    """Weather, hours, own staff, health and safety and notes: the entry's own fields."""

    class Meta:
        model = DiaryEntry
        fields = ['weather_am', 'weather_pm', 'weather_note', 'site_start', 'site_finish', 'own_staff_count',
                  'own_staff', 'hs_incident', 'hs_notes', 'notes']
        widgets = {'site_start': forms.TextInput(attrs={'type': 'time'}),
                   'site_finish': forms.TextInput(attrs={'type': 'time'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        phone_friendly(self)

    def clean(self):
        data = super().clean()
        if data.get('hs_incident') and not (data.get('hs_notes') or '').strip():
            self.add_error('hs_notes', 'Say what happened, who was involved and what was done about it.')
        start, finish = data.get('site_start'), data.get('site_finish')
        if start and finish and finish <= start:
            self.add_error('site_finish', 'The site must close after it opens.')
        return data


class LineForm(StyledModelForm):
    def __init__(self, *args, project=None, **kwargs):
        self.project = project
        super().__init__(*args, **kwargs)
        phone_friendly(self)


class LabourForm(LineForm):
    class Meta:
        model = LabourLine
        fields = ['package', 'name', 'trade', 'headcount', 'notes']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['package'].queryset = Package.objects.filter(
            project=self.project, status=Package.APPOINTED).select_related('subcontractor', 'trade')
        self.fields['package'].label_from_instance = lambda p: (
            f'{p.subcontractor.company_name}{f" ({p.trade.name})" if p.trade_id else ""}')
        self.fields['package'].empty_label = 'Pick a subcontractor'
        self.fields['headcount'].widget.attrs.update({'min': 1, 'max': 500})

    def clean(self):
        data = super().clean()
        if not data.get('package') and not (data.get('name') or '').strip():
            self.add_error('name', 'Pick a subcontractor from the list or type a name.')
        if data.get('headcount') == 0:
            self.add_error('headcount', 'At least one person.')
        return data


class PlantForm(LineForm):
    class Meta:
        model = PlantLine
        fields = ['description', 'quantity', 'supplier', 'notes']


class DeliveryForm(LineForm):
    class Meta:
        model = DeliveryLine
        fields = ['supplier', 'description', 'arrived_at', 'notes']
        widgets = {'arrived_at': TIME}


class VisitorForm(LineForm):
    class Meta:
        model = VisitorLine
        fields = ['name', 'company', 'reason', 'arrived_at']
        widgets = {'arrived_at': TIME}


class InstructionForm(LineForm):
    class Meta:
        model = InstructionLine
        fields = ['given_by', 'description', 'rfi', 'change_event']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['rfi'].queryset = RFI.objects.filter(project=self.project)
        self.fields['rfi'].label_from_instance = lambda r: f'{r.reference} {r.title}'
        self.fields['change_event'].queryset = ChangeEvent.objects.filter(project=self.project)
        self.fields['change_event'].label_from_instance = lambda c: f'{c.reference} {c.title}'


class DelayForm(LineForm):
    class Meta:
        model = DelayLine
        fields = ['cause', 'hours_lost', 'description', 'trades_affected']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['hours_lost'].widget.attrs.update({'min': '0.5', 'max': '24', 'step': '0.5'})

    def clean_hours_lost(self):
        h = self.cleaned_data.get('hours_lost')
        if h is not None and not 0 < h <= 24:
            raise forms.ValidationError('Between 0.5 and 24 hours.')
        return h


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput(attrs={'accept': 'image/*', 'multiple': True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else [data] if data else []
        return [super(MultipleImageField, self).clean(f, initial) for f in files]


class PhotoUploadForm(forms.Form):
    """Several photos at once from the camera or the phone's library, with one caption and area for the lot."""
    camera = MultipleImageField(label='Take photos', required=False)
    library = MultipleImageField(label='Or choose from your phone', required=False)
    caption = forms.CharField(max_length=200, required=False, help_text='Applies to every photo in this upload.')
    area = forms.CharField(label='Area or location', max_length=120, required=False)
    taken_on = forms.DateField(label='Date', widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    attach = forms.BooleanField(label='Add to that day\'s site diary', required=False, initial=True)

    def __init__(self, *args, areas=(), **kwargs):
        super().__init__(*args, **kwargs)
        # The phone camera straight away; the second picker for photos taken earlier
        self.fields['camera'].widget.attrs['capture'] = 'environment'
        self.fields['area'].widget.attrs['list'] = 'diary-areas'
        self.areas = areas

    def clean_taken_on(self):
        d = self.cleaned_data['taken_on']
        if d > timezone.localdate():
            raise forms.ValidationError('That date is in the future.')
        return d

    def clean(self):
        data = super().clean()
        files = (data.get('camera') or []) + (data.get('library') or [])
        if not files and not self.errors:
            raise forms.ValidationError('Take or choose at least one photo.')
        if len(files) > MAX_FILES:
            raise forms.ValidationError(f'Up to {MAX_FILES} photos at a time.')
        for f in files:
            try:
                validate_photo(f)
            except forms.ValidationError as e:
                self.add_error(None, e)
        data['files'] = files
        return data


class PhotoForm(StyledModelForm):
    class Meta:
        model = SitePhoto
        fields = ['caption', 'area', 'taken_on', 'shared_with_client']
        help_texts = {'shared_with_client': 'The client sees it, read only, on their project overview. '
                                            'Nobody else outside the company does.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['area'].widget.attrs['list'] = 'diary-areas'


class UnlockForm(forms.Form):
    reason = forms.CharField(label='Why it needs changing', max_length=500,
                             widget=forms.Textarea(attrs={'rows': 2}),
                             help_text='Kept in the audit trail with your name.')


class SettingsForm(forms.ModelForm):
    class Meta:
        model = DiarySettings
        fields = ['saturday_counts']
