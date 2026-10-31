from django import forms
from django.db.models import Q
from django.core.exceptions import ValidationError

from core.forms import RecordFormMixin
from directory.models import Package
from projects.access import staff_users
from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets

from .models import ProcurementSettings, ScheduleLine, Submittal, SubmittalRevision

# Drawings, data sheets, photos of samples, O&M manuals. Anything else is still checked by validate_upload.
ACCEPT = '.pdf,.dwg,.dxf,.ifc,.jpg,.jpeg,.png,.heic,.webp,.doc,.docx,.xls,.xlsx,.zip,image/*'
MAX_FILES = 20


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Several files in one input; each goes through projects.files.validate_upload."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput(attrs={'accept': ACCEPT}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        items = [d for d in (data if isinstance(data, (list, tuple)) else [data]) if d]
        if not items:
            if self.required:
                raise ValidationError('Add at least one file.')
            return []
        if len(items) > MAX_FILES:
            raise ValidationError(f'Up to {MAX_FILES} files at a time.')
        out = []
        for f in items:
            f = super().clean(f, initial)
            validate_upload(f)
            out.append(f)
        return out


class StyledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)


def project_packages(project, keep=None):
    """Packages still in play on the job (invited, quoted, appointed), plus `keep` (the one already chosen)."""
    return Package.objects.filter(Q(project=project) & (~Q(status__in=Package.CLOSED_STATUSES) | Q(pk=keep))) \
        .select_related('subcontractor', 'trade')


def appointed_packages(project, keep=None):
    """Submittals come from appointed subcontractors only; `keep` holds on to the one already chosen."""
    return Package.objects.filter(Q(project=project) & (Q(status=Package.APPOINTED) | Q(pk=keep))) \
        .select_related('subcontractor', 'trade')


def blocking_submittals(line):
    """Submittals on the line that are still live and not A or B: the line can't be ordered yet."""
    if line is None or not line.pk:
        return Submittal.objects.none()
    return line.submittals.exclude(status__in=Submittal.ACCEPTED + (Submittal.WITHDRAWN,))


def package_label(p):
    return f'{p.trade or "Package"}: {p.subcontractor} ({p.get_status_display().lower()})'


class ScheduleLineForm(StyledModelForm):
    class Meta:
        model = ScheduleLine
        fields = ['title', 'package', 'long_lead', 'supplier', 'required_on_site', 'lead_time_weeks', 'approval_weeks',
                  'design_info_by', 'status', 'ordered_on', 'notes']
        widgets = {'notes': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['package'].queryset = project_packages(project, keep=self.instance.package_id)
        self.fields['package'].label_from_instance = package_label
        self.fields['title'].widget.attrs['placeholder'] = 'e.g. Bar joinery, kitchen extract, feature lighting'
        for name in ('lead_time_weeks', 'approval_weeks'):
            self.fields[name].widget.attrs.update({'min': 0, 'inputmode': 'numeric'})
        if self.instance.pk:
            # A line only moves forward: offer its current status and the later ones
            rank = ScheduleLine.RANK[self.instance.status]
            self.fields['status'].choices = [(c, label) for c, label in ScheduleLine.STATUS_CHOICES
                                             if ScheduleLine.RANK[c] >= rank]

    def clean_status(self):
        status = self.cleaned_data['status']
        if status in ScheduleLine.DONE_STATUSES and blocking_submittals(self.instance).exists():
            raise ValidationError('Submittals on this line are still waiting for an A or B. Get them approved '
                                  '(or withdraw them) before marking it ordered.')
        return status


class SubmittalForm(RecordFormMixin, StyledModelForm):
    class Meta:
        model = Submittal
        fields = ['title', 'kind', 'line', 'package', 'spec_reference', 'description', 'due_date']
        labels = {'title': 'What it is', 'due_date': 'Submit by'}
        help_texts = {'due_date': "Leave blank to use the procurement line's submittals-due date."}
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['title'].widget.attrs['placeholder'] = 'e.g. Bar back shop drawings'
        self.fields['line'].queryset = ScheduleLine.objects.filter(project=project)
        self.fields['package'].queryset = appointed_packages(project, keep=self.instance.package_id)
        self.fields['package'].label_from_instance = package_label
        self.fields['package'].help_text = ("Appointed packages only: the subcontractor uploads it in their portal. "
                                            "Leave blank to use the procurement line's package.")

    def clean(self):
        data = super().clean()
        line, package = data.get('line'), data.get('package')
        if line is None and package is None:
            raise ValidationError('Link it to a procurement line or a trade package.')
        if package is None and line is not None and line.package_id:
            if line.package.status != Package.APPOINTED and line.package_id != self.instance.package_id:
                raise ValidationError(f'{line.package.subcontractor} isn\'t appointed on this line\'s package yet. '
                                      'Appoint them first, or ask for it under a different package.')
            data['package'] = line.package
            self.instance.package = line.package
        if data.get('due_date') is None and line is not None:
            data['due_date'] = line.submittals_due
            self.instance.due_date = line.submittals_due
        return data


class SubmitForm(StyledForm):
    files = MultipleFileField(label='Files', help_text='Drawings, data sheets, photos of samples or O&M documents.')
    note = forms.CharField(label='Note', required=False, max_length=2000, widget=forms.Textarea(attrs={'rows': 3}))


class ReviewForm(StyledForm):
    code = forms.ChoiceField(label='Status', choices=SubmittalRevision.CODE_CHOICES, widget=forms.RadioSelect)
    comments = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 4}), max_length=5000)
    markups = MultipleFileField(label='Marked-up copy (optional)', required=False)

    def clean(self):
        data = super().clean()
        if data.get('code') in ('B', 'C') and not (data.get('comments') or '').strip():
            self.add_error('comments', 'Say what needs to change.' if data['code'] == 'C'
                           else 'Give the comments they must incorporate.')
        return data


class ForwardForm(StyledForm):
    consultant = forms.ModelChoiceField(label='Designer or consultant', queryset=None)
    note = forms.CharField(label='Note to them', required=False, max_length=2000,
                           widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .access import consultants
        self.fields['consultant'].queryset = consultants(project)
        self.fields['consultant'].label_from_instance = lambda u: u.full_name or u.email


class DesignerReplyForm(StyledForm):
    comments = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}), max_length=5000)
    files = MultipleFileField(label='Marked-up copy (optional)', required=False)


class SettingsForm(StyledModelForm):
    class Meta:
        model = ProcurementSettings
        fields = ['review_days', 'resubmit_days', 'reviewer']

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['reviewer'].queryset = staff_users(org, 'delivery')
        self.fields['reviewer'].label_from_instance = lambda u: u.full_name or u.email
