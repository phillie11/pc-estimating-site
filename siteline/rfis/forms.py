from django import forms

from core.forms import RecordFormMixin
from projects.access import assignable_users
from projects.files import validate_upload
from projects.forms import StyledModelForm

from .models import RFI


class RFIForm(RecordFormMixin, StyledModelForm):
    # A tick box over the record's visibility, as it was before RFIs moved onto RecordBase
    is_shared = forms.BooleanField(label='Visible to project collaborators', required=False, initial=True)

    class Meta:
        model = RFI
        fields = ['title', 'question', 'category', 'priority', 'ball_in_court_user', 'due_date', 'document_reference',
                  'cost_impact', 'programme_impact_days', 'attachment']
        labels = {'title': 'Subject', 'ball_in_court_user': 'Ball in court'}
        help_texts = {'ball_in_court_user': 'Who needs to answer this.'}
        widgets = {'question': forms.Textarea(attrs={'rows': 6})}

    def __init__(self, *args, project=None, external=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['ball_in_court_user'].queryset = assignable_users(project)
        if self.instance.pk:
            self.initial['is_shared'] = self.instance.is_shared
        # Keep the tick box where it always was in the form, after programme impact
        self.fields['is_shared'] = self.fields.pop('is_shared')
        self.fields['attachment'] = self.fields.pop('attachment')
        if external:
            for f in ('is_shared', 'cost_impact', 'programme_impact_days'):
                self.fields.pop(f)

    def clean_attachment(self):
        f = self.cleaned_data.get('attachment')
        if f and hasattr(f, 'size'):
            validate_upload(f)
        return f

    def save(self, commit=True):
        if 'is_shared' in self.fields:
            self.instance.is_shared = self.cleaned_data['is_shared']
        return super().save(commit)


class RFIResponseForm(forms.Form):
    response = forms.CharField(widget=forms.Textarea(attrs={'rows': 5}))
    close = forms.BooleanField(required=False, label='Close the RFI with this response')

    def __init__(self, *args, external=False, **kwargs):
        super().__init__(*args, **kwargs)
        if external:
            del self.fields['close']


class ReassignForm(forms.Form):
    ball_in_court_user = forms.ModelChoiceField(queryset=None, label='Ball in court')

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['ball_in_court_user'].queryset = assignable_users(project)
