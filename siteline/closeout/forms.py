from django import forms
from django.core.exceptions import ValidationError

from directory.models import Package
from projects.access import assignable_users
from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets
from projects.models import Issue

from .models import Certificate, CompletionSection, HandoverPack, OMDocument, OMSection, OMTemplateSection

DOC_ACCEPT = '.pdf,.jpg,.jpeg,.png,.heic,.webp,.doc,.docx,.xls,.xlsx,.zip,image/*'
PHOTO_ACCEPT = 'image/*'


def _check_file(f):
    if f and hasattr(f, 'size'):
        validate_upload(f)
    return f


class CompletionForm(StyledModelForm):
    class Meta:
        model = CompletionSection
        fields = ['name', 'pc_date', 'defects_period_months', 'made_good_on', 'certificate', 'notes']
        widgets = {'notes': forms.Textarea(attrs={'rows': 2}),
                   'certificate': forms.ClearableFileInput(attrs={'accept': DOC_ACCEPT})}

    def __init__(self, *args, months_hint=None, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.is_whole_works:
            del self.fields['name']
        if months_hint:
            self.fields['defects_period_months'].help_text = (
                f"Leave blank to use the contract's {months_hint} months.")
        else:
            self.fields['defects_period_months'].help_text = (
                'No defects period is set on the contract. Type it here, or set it on the contract.')

    def clean_certificate(self):
        return _check_file(self.cleaned_data.get('certificate'))

    def clean(self):
        data = super().clean()
        pc, made_good = data.get('pc_date'), data.get('made_good_on')
        if made_good and not pc:
            self.add_error('made_good_on', 'Add the practical completion date first.')
        elif made_good and pc and made_good < pc:
            self.add_error('made_good_on', 'This is before practical completion. Check the date on the certificate.')
        name = data.get('name')
        if name and CompletionSection.objects.filter(project=self.instance.project, name__iexact=name) \
                .exclude(pk=self.instance.pk).exists():
            self.add_error('name', 'There is already a section with this name on this job.')
        return data


class SectionAddForm(StyledModelForm):
    class Meta:
        model = CompletionSection
        fields = ['name', 'pc_date']
        labels = {'name': 'Section name'}
        help_texts = {'name': 'For sectional completion, e.g. "Ground floor bar".', 'pc_date': 'Add it now or later.'}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields['name'].initial = ''

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise ValidationError('Give the section a name.')
        if CompletionSection.objects.filter(project=self.project, name__iexact=name).exists():
            raise ValidationError('There is already a section with this name on this job.')
        return name


class DefectForm(StyledModelForm):
    """Reporting a defect: what and where are all that's needed. A photo helps."""

    class Meta:
        model = Issue
        fields = ['title', 'location', 'photo', 'description']
        labels = {'title': "What's wrong", 'location': 'Where is it', 'photo': 'Photo',
                  'description': 'Anything else we should know'}
        help_texts = {'title': 'For example "Door to WC 2 does not close".',
                      'location': 'Room, floor or area, e.g. "First floor kitchen".'}
        widgets = {'photo': forms.ClearableFileInput(attrs={'accept': PHOTO_ACCEPT, 'capture': 'environment'}),
                   'description': forms.Textarea(attrs={'rows': 3})}
        error_messages = {'title': {'required': 'Say what is wrong, for example "Door to WC 2 does not close".'}}

    def __init__(self, *args, project=None, staff=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['location'].required = True
        self.fields['location'].error_messages['required'] = 'Say where it is: a room, floor or area.'
        if staff:
            self.fields['assignee'] = forms.ModelChoiceField(
                assignable_users(project), required=False, label='Who will fix it',
                help_text='Usually the subcontractor. You can do this later.')
            self.fields['due_date'] = forms.DateField(required=False, label='Fix by')
            style_widgets(self)

    def clean_photo(self):
        return _check_file(self.cleaned_data.get('photo'))


class OMSectionForm(StyledModelForm):
    class Meta:
        model = OMSection
        fields = ['title', 'package', 'contents', 'not_needed']
        widgets = {'contents': forms.Textarea(attrs={'rows': 4})}
        error_messages = {'title': {'required': 'Give the section a name, for example "Joinery".'}}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        keep = self.instance.package_id
        qs = Package.objects.filter(project=project, status=Package.APPOINTED) | Package.objects.filter(pk=keep)
        self.fields['package'].queryset = qs.select_related('subcontractor', 'trade').distinct()
        self.fields['package'].label_from_instance = lambda p: f'{p.subcontractor} ({p.trade or "package"})'
        self.fields['package'].empty_label = 'We provide it ourselves'


class OMRequestForm(forms.Form):
    due_date = forms.DateField(label='Send it by', help_text='Suggested: two weeks before completion.',
                               error_messages={'required': 'Pick the date you need it by.',
                                               'invalid': 'Enter a date, for example 14/12/2026.'})
    note = forms.CharField(label='Note to the subcontractor', required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)


class OMDocumentForm(StyledModelForm):
    class Meta:
        model = OMDocument
        fields = ['title', 'file']
        help_texts = {'title': 'For example "Ceiling tile data sheet".'}
        widgets = {'file': forms.ClearableFileInput(attrs={'accept': DOC_ACCEPT})}
        error_messages = {'title': {'required': 'Say what the file is, for example "Ceiling tile data sheet".'},
                          'file': {'required': 'Choose the file to add.'}}

    def clean_file(self):
        return _check_file(self.cleaned_data.get('file'))


class CertificateForm(StyledModelForm):
    class Meta:
        model = Certificate
        fields = ['kind', 'file', 'issued_by', 'issued_on', 'title']
        widgets = {'file': forms.ClearableFileInput(attrs={'accept': DOC_ACCEPT, 'capture': 'environment'})}
        labels = {'file': 'Certificate (PDF or photo)'}
        error_messages = {'file': {'required': 'Choose the certificate: a PDF, or a photo of it.'}}

    def clean_file(self):
        return _check_file(self.cleaned_data.get('file'))


class PackForm(StyledModelForm):
    class Meta:
        model = HandoverPack
        fields = ['note']
        widgets = {'note': forms.Textarea(attrs={'rows': 2, 'placeholder': 'Optional, e.g. "Handover at practical '
                                                                            'completion"'})}


class TemplateSectionForm(StyledModelForm):
    class Meta:
        model = OMTemplateSection
        fields = ['title', 'trade', 'contents', 'order']
        widgets = {'contents': forms.Textarea(attrs={'rows': 3})}


TemplateFormSet = forms.modelformset_factory(OMTemplateSection, form=TemplateSectionForm, extra=1, can_delete=True)
