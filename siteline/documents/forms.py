from django import forms
from django.conf import settings

from core.forms import RecordFormMixin
from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets
from projects.models import Document, DocumentRevision

from . import naming
from .models import CodeList, DistributionRule, RegisterSettings, Transmittal, check_number, code_lists

ACCEPT = '.pdf,.dwg,.dxf,.ifc,.rvt,.nwd,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.jpg,.jpeg,.png,.zip,.txt,.csv'
STATUS_CHOICES = [('', 'Work it out from the suitability'), (DocumentRevision.WIP, 'Work in progress (internal)'),
                  (DocumentRevision.SHARED, 'Shared'), (DocumentRevision.PUBLISHED, 'Published')]


def suitability_field(project):
    rows = code_lists(project)['suitability']
    return forms.ChoiceField(choices=[('', 'Not set')] + [(c, f'{c} – {d}') for c, d in rows], required=False,
                             label='Suitability')


class NumberCheckMixin:
    """
    Checks the document number against ISO 19650 and the project's code lists. Staff must
    tick "use this number anyway" to save one that doesn't conform; externals' uploads are
    never held up, but the document is flagged for staff to check.
    """
    def init_number(self, project, external):
        self.project, self.external = project, external
        self.number_warnings = []
        # Hidden until a number with warnings is posted, then shown as a tick box under it
        self.fields['accept_naming'] = forms.BooleanField(
            required=False, label='Use this number anyway', widget=forms.HiddenInput,
            help_text='It doesn\'t fully follow the project\'s naming. Tick to keep it as typed.')
        self.fields['reference'].label = 'Document number'
        self.fields['reference'].help_text = 'e.g. SLN-ABC-ZZ-02-DR-A-1001 (project-originator-volume-level-type-role-number)'

    def clean_number(self, cleaned):
        ref = (cleaned.get('reference') or '').strip()
        _, self.number_warnings = check_number(self.project, ref)
        if self.number_warnings and not self.external:
            self.fields['accept_naming'].widget = forms.CheckboxInput()
        if self.number_warnings and not self.external and not cleaned.get('accept_naming'):
            for w in self.number_warnings:
                self.add_error('reference', w)
        return cleaned

    def apply_number(self, doc):
        doc.set_number(self.cleaned_data.get('reference', ''), self.number_warnings,
                       accepted=bool(self.cleaned_data.get('accept_naming')))


class DocumentUploadForm(NumberCheckMixin, StyledModelForm):
    """A new document with its first revision."""
    file = forms.FileField(widget=forms.ClearableFileInput(attrs={'accept': ACCEPT}))
    revision = forms.CharField(max_length=20, initial='P01')
    status = forms.ChoiceField(choices=STATUS_CHOICES, required=False)
    notes = forms.CharField(max_length=300, required=False, label='Revision notes')
    shared_with = forms.MultipleChoiceField(choices=Document.SHARE_ROLE_CHOICES, required=False,
                                            widget=forms.CheckboxSelectMultiple, label='Shared with',
                                            help_text='Only applies when shared. None ticked means everyone on the project.')
    issue_now = forms.BooleanField(required=False, label='Issue to usual recipients now',
                                   help_text='Sends a transmittal and notifies them.')
    purpose = forms.ChoiceField(choices=Transmittal.PURPOSE_CHOICES, initial=Transmittal.PURPOSE_INFORMATION, required=False,
                                label='Issued')

    class Meta:
        model = Document
        fields = ['title', 'reference', 'category', 'discipline', 'visibility', 'tender_issue']

    def __init__(self, *args, project=None, external=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.init_number(project, external)
        self.fields['suitability'] = suitability_field(project)
        self.fields['file'].help_text = f'Max {settings.MAX_UPLOAD_MB} MB.'
        order = ['title', 'reference', 'accept_naming', 'category', 'discipline', 'file', 'revision', 'suitability',
                 'status', 'notes', 'visibility', 'shared_with', 'tender_issue', 'issue_now', 'purpose']
        self.order_fields(order)
        if external:
            for name in ('visibility', 'shared_with', 'tender_issue', 'issue_now', 'purpose', 'status', 'accept_naming'):
                del self.fields[name]
        style_widgets(self)

    def clean_file(self):
        f = self.cleaned_data['file']
        validate_upload(f)
        return f

    def clean(self):
        return self.clean_number(super().clean())


class RevisionUploadForm(forms.ModelForm):
    make_current = forms.BooleanField(required=False, label='Make this the current revision',
                                      help_text='Only needed for an older revision: a newer one becomes current anyway.')
    status = forms.ChoiceField(choices=STATUS_CHOICES, required=False)
    issue_now = forms.BooleanField(required=False, label='Issue to usual recipients now',
                                   help_text='Sends a transmittal and notifies them.')
    purpose = forms.ChoiceField(choices=Transmittal.PURPOSE_CHOICES, initial=Transmittal.PURPOSE_INFORMATION, required=False,
                                label='Issued')

    class Meta:
        model = DocumentRevision
        fields = ['file', 'revision', 'notes']
        widgets = {'file': forms.ClearableFileInput(attrs={'accept': ACCEPT})}

    def __init__(self, *args, project=None, external=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['suitability'] = suitability_field(project)
        self.order_fields(['file', 'revision', 'suitability', 'status', 'notes', 'make_current', 'issue_now', 'purpose'])
        if external:
            for name in ('status', 'issue_now', 'purpose'):
                del self.fields[name]

    def clean_file(self):
        f = self.cleaned_data['file']
        validate_upload(f)
        return f


class DocumentDetailsForm(NumberCheckMixin, StyledModelForm):
    shared_with = forms.MultipleChoiceField(choices=Document.SHARE_ROLE_CHOICES, required=False,
                                            widget=forms.CheckboxSelectMultiple, label='Shared with',
                                            help_text='Only applies when shared. None ticked means everyone on the project.')

    class Meta:
        model = Document
        fields = ['title', 'reference', 'category', 'discipline', 'sheet_size', 'scale', 'folder', 'visibility',
                  'tender_issue']

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.init_number(project, external=False)
        self.fields['shared_with'].initial = self.instance.share_role_list
        self.order_fields(['title', 'reference', 'accept_naming'])

    def clean(self):
        return self.clean_number(super().clean())

    def save(self, commit=True):
        doc = super().save(commit=False)
        self.apply_number(doc)
        doc.set_share_roles(self.cleaned_data.get('shared_with'))
        if commit:
            doc.save()
        return doc


class TransmittalForm(RecordFormMixin, StyledModelForm):
    revisions = forms.ModelMultipleChoiceField(queryset=DocumentRevision.objects.none(), label='Documents',
                                               widget=forms.CheckboxSelectMultiple, required=False)
    users = forms.ModelMultipleChoiceField(queryset=None, label='People', widget=forms.CheckboxSelectMultiple,
                                           required=False)
    companies = forms.ModelMultipleChoiceField(queryset=None, label='Companies', widget=forms.CheckboxSelectMultiple,
                                               required=False,
                                               help_text='Anyone from the company on this project can open and acknowledge it.')

    class Meta:
        model = Transmittal
        fields = ['title', 'purpose', 'due_date', 'cover_note']
        labels = {'title': 'Subject'}
        widgets = {'cover_note': forms.Textarea(attrs={'rows': 4})}

    def __init__(self, *args, project=None, **kwargs):
        from .services import recipient_choices
        super().__init__(*args, **kwargs)
        users, companies = recipient_choices(project)
        self.fields['users'].queryset = users
        self.fields['companies'].queryset = companies
        self.fields['users'].label_from_instance = lambda u: u.full_name or u.email
        self.fields['revisions'].queryset = DocumentRevision.objects.filter(document__project=project) \
            .exclude(status=DocumentRevision.WITHDRAWN).select_related('document') \
            .order_by('document__reference', 'document__title', '-uploaded_at')
        self.fields['revisions'].label_from_instance = lambda r: (
            f'{r.document.label} · rev {r.revision}{" " + r.suitability if r.suitability else ""}'
            f'{"" if r.is_current else " (not current)"}')


class RegisterSettingsForm(StyledModelForm):
    class Meta:
        model = RegisterSettings
        fields = ['project_code', 'filename_pattern', 'check_naming']
        widgets = {'filename_pattern': forms.TextInput(attrs={'placeholder': naming.DEFAULT_PATTERN})}


class CodeForm(StyledModelForm):
    class Meta:
        model = CodeList
        fields = ['field', 'code', 'description']

    def clean_code(self):
        code = naming.clean_code(self.cleaned_data['code'])
        if not code.isalnum():
            raise forms.ValidationError('Letters and numbers only.')
        return code


class DistributionRuleForm(StyledModelForm):
    class Meta:
        model = DistributionRule
        fields = ['discipline', 'user', 'subcontractor']
        labels = {'user': 'Person', 'subcontractor': 'Company'}

    def __init__(self, *args, project=None, **kwargs):
        from .services import recipient_choices
        super().__init__(*args, **kwargs)
        users, companies = recipient_choices(project)
        self.fields['user'].queryset = users
        self.fields['subcontractor'].queryset = companies

    def clean(self):
        cleaned = super().clean()
        if bool(cleaned.get('user')) == bool(cleaned.get('subcontractor')):
            raise forms.ValidationError('Pick a person or a company.')
        return cleaned
