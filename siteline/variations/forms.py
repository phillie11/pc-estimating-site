from django import forms

from projects.files import validate_upload
from projects.forms import StyledModelForm
from rfis.models import RFI

from .models import Variation


class VariationForm(StyledModelForm):
    class Meta:
        model = Variation
        fields = ['title', 'cause', 'description', 'value', 'time_impact_days', 'proceed_at_risk',
                  'client_reference', 'rfi', 'attachment']
        widgets = {'description': forms.Textarea(attrs={'rows': 6})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['rfi'].queryset = RFI.objects.filter(project=project)

    def clean_attachment(self):
        f = self.cleaned_data.get('attachment')
        if f and hasattr(f, 'size'):
            validate_upload(f)
        return f


class DecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=[('APPROVED', 'Approve'), ('REJECTED', 'Reject')], widget=forms.RadioSelect)
    approved_value = forms.DecimalField(max_digits=14, decimal_places=2, required=False,
                                        help_text='Leave blank to approve at the quoted value.')
    client_reference = forms.CharField(max_length=120, required=False, label='Your instruction / PO ref')
    decision_notes = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False, label='Notes')
