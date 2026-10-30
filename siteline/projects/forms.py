from django import forms
from django.utils import timezone

from .access import assignable_users, staff_users
from .files import validate_upload
from .models import Activity, Comment, Issue, Project, ProjectInvite, ProjectMember, Task


def style_widgets(form):
    """Native date pickers and sensible textarea sizes for every form."""
    for name, field in form.fields.items():
        w = field.widget
        if isinstance(field, forms.DateTimeField):
            field.widget = forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')
            field.input_formats = ['%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M']
        elif isinstance(field, forms.DateField):
            field.widget = forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')
        elif isinstance(w, forms.Textarea):
            w.attrs.setdefault('rows', 4)


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)


class ProjectForm(StyledModelForm):
    class Meta:
        model = Project
        fields = ['name', 'client_name', 'site_address', 'sector', 'stage', 'owner', 'manager', 'value', 'probability',
                  'tender_received', 'tender_due', 'site_visit', 'queries_close', 'quotes_due',
                  'programme_due', 'quality_due', 'logistics_due',
                  'start_date', 'completion_date', 'lead_source', 'description', 'outcome_notes']

    def __init__(self, *args, org=None, membership=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['owner'].queryset = staff_users(org, 'estimating')
        self.fields['manager'].queryset = staff_users(org, 'delivery')
        self.fields['probability'].widget.attrs.update({'min': 0, 'max': 100})
        if membership is not None:
            # People only create and move work within the sections they work in
            allowed = [c for c in Project.STAGE_CHOICES
                       if (c[0] in Project.ESTIMATING_STAGES and membership.can_estimate)
                       or (c[0] not in Project.ESTIMATING_STAGES and membership.can_deliver)
                       or (self.instance.pk and c[0] == self.instance.stage)]
            self.fields['stage'].choices = allowed
            if not membership.can_deliver:
                del self.fields['manager']

    def clean_probability(self):
        p = self.cleaned_data.get('probability')
        if p is not None and not 0 <= p <= 100:
            raise forms.ValidationError('Between 0 and 100.')
        return p

    def clean(self):
        data = super().clean()
        received, due = data.get('tender_received'), data.get('tender_due')
        if received and due and received > timezone.localdate(due):
            self.add_error('tender_received', 'The enquiry must arrive before the tender return.')
        start, completion = data.get('start_date'), data.get('completion_date')
        if start and completion and completion < start:
            self.add_error('completion_date', 'Completion cannot be before the start on site.')
        return data


class HandoverForm(StyledModelForm):
    """Estimating hands a won job to the projects team."""
    class Meta:
        model = Project
        fields = ['manager', 'value', 'start_date', 'completion_date', 'handover_notes']
        labels = {'value': 'Contract sum'}
        widgets = {'handover_notes': forms.Textarea(attrs={'rows': 6})}

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['manager'].queryset = staff_users(org, 'delivery')
        self.fields['manager'].required = True

    def clean(self):
        data = super().clean()
        if data.get('start_date') and data.get('completion_date') and data['completion_date'] < data['start_date']:
            self.add_error('completion_date', 'Completion cannot be before the start on site.')
        return data


class TaskForm(StyledModelForm):
    class Meta:
        model = Task
        fields = ['title', 'assignee', 'due_date', 'priority', 'status', 'description']

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assignee'].queryset = assignable_users(project)


class ActivityForm(StyledModelForm):
    class Meta:
        model = Activity
        fields = ['name', 'responsible', 'start', 'finish', 'progress', 'after', 'is_milestone', 'is_shared']

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        others = Activity.objects.filter(project=project)
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        self.fields['after'].queryset = others
        self.fields['finish'].required = False
        self.fields['progress'].widget.attrs.update({'min': 0, 'max': 100, 'step': 5})

    def clean_progress(self):
        p = self.cleaned_data.get('progress')
        if p is not None and not 0 <= p <= 100:
            raise forms.ValidationError('Between 0 and 100.')
        return p

    def clean(self):
        data = super().clean()
        start, finish = data.get('start'), data.get('finish')
        if start and (data.get('is_milestone') or not finish):
            data['finish'] = start
        elif start and finish and finish < start:
            self.add_error('finish', 'Finish cannot be before the start.')
        return data


class IssueForm(StyledModelForm):
    class Meta:
        model = Issue
        fields = ['title', 'issue_type', 'location', 'priority', 'assignee', 'due_date', 'description', 'photo']

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assignee'].queryset = assignable_users(project)

    def clean_photo(self):
        f = self.cleaned_data.get('photo')
        if f and hasattr(f, 'size'):
            validate_upload(f)
        return f


# Document upload forms live in documents/forms.py


class ProjectInviteForm(StyledModelForm):
    class Meta:
        model = ProjectInvite
        fields = ['email', 'role', 'company_name', 'message']
        widgets = {'message': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Optional note included in the email'})}

    def __init__(self, *args, project=None, **kwargs):
        self.project = project
        super().__init__(*args, **kwargs)
        # Tendering subcontractors are invited from the packages page, against a package
        self.fields['role'].choices = ProjectMember.EXTERNAL_ROLE_CHOICES

    def clean_email(self):
        email = self.cleaned_data['email'].lower()
        if ProjectMember.objects.filter(project=self.project, user__email__iexact=email).exists():
            raise forms.ValidationError('Already on this project.')
        return email


class AddStaffForm(forms.Form):
    user = forms.ModelChoiceField(queryset=None, label='Team member')
    role = forms.ChoiceField(choices=ProjectMember.INTERNAL_ROLE_CHOICES)

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        existing = project.members.values_list('user_id', flat=True)
        self.fields['user'].queryset = staff_users(project.organization).exclude(pk__in=existing)


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ['body', 'internal_only']
        widgets = {'body': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Add a comment…'})}
        labels = {'body': '', 'internal_only': 'Internal note (hidden from collaborators)'}

    def __init__(self, *args, external=False, **kwargs):
        super().__init__(*args, **kwargs)
        if external:
            del self.fields['internal_only']
