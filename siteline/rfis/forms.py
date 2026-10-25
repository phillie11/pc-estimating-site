from django import forms

from projects.access import assignable_users
from projects.files import validate_upload
from projects.forms import StyledModelForm

from .models import RFI


class RFIForm(StyledModelForm):
    class Meta:
        model = RFI
        fields = ['subject', 'question', 'category', 'priority', 'assigned_to', 'due_date', 'document_reference',
                  'cost_impact', 'programme_impact_days', 'is_shared', 'attachment']
        widgets = {'question': forms.Textarea(attrs={'rows': 6})}

    def __init__(self, *args, project=None, external=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assigned_to'].queryset = assignable_users(project)
        if external:
            for f in ('is_shared', 'cost_impact', 'programme_impact_days'):
                self.fields.pop(f)

    def clean_attachment(self):
        f = self.cleaned_data.get('attachment')
        if f and hasattr(f, 'size'):
            validate_upload(f)
        return f


class RFIResponseForm(forms.Form):
    response = forms.CharField(widget=forms.Textarea(attrs={'rows': 5}))
    close = forms.BooleanField(required=False, label='Close the RFI with this response')

    def __init__(self, *args, external=False, **kwargs):
        super().__init__(*args, **kwargs)
        if external:
            del self.fields['close']


class ReassignForm(forms.Form):
    assigned_to = forms.ModelChoiceField(queryset=None, label='Ball in court')

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assigned_to'].queryset = assignable_users(project)
