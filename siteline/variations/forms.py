from django import forms

from core.forms import RecordFormMixin
from projects.files import validate_upload
from projects.forms import StyledModelForm
from rfis.models import RFI

from .models import Variation


class VariationForm(RecordFormMixin, StyledModelForm):
    class Meta:
        model = Variation
        fields = ['title', 'cause', 'description', 'value', 'time_impact_days', 'eot_days_requested', 'eot_days_granted',
                  'quotation_due',
                  'proceed_at_risk', 'client_reference', 'rfi', 'attachment']
        widgets = {'description': forms.Textarea(attrs={'rows': 6})}

    def __init__(self, *args, project=None, vocab=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['rfi'].queryset = RFI.objects.filter(project=project)
        self.fields['value'].widget.attrs.setdefault('inputmode', 'decimal')
        if vocab:
            words = vocab['time_extension'].capitalize()
            self.fields['eot_days_requested'].label = f'{words} requested (days)'
            self.fields['eot_days_granted'].label = f'{words} granted (days)'
        if not self.instance.pk or self.instance.status == Variation.STATUS_DRAFT:
            del self.fields['eot_days_granted']  # nothing is granted before the client has seen it

    def clean_attachment(self):
        f = self.cleaned_data.get('attachment')
        if f and hasattr(f, 'size'):
            validate_upload(f)
        return f


class DecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=[('APPROVED', 'Approve'), ('REJECTED', 'Reject')], widget=forms.RadioSelect)
    approved_value = forms.DecimalField(max_digits=14, decimal_places=2, required=False,
                                        help_text='Leave blank to approve at the quoted value.')
    eot_days_granted = forms.IntegerField(label='Extension of time granted (days)', required=False, min_value=0,
                                          help_text='Leave blank if none.')
    client_reference = forms.CharField(max_length=120, required=False, label='Your instruction / PO ref')
    decision_notes = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False, label='Notes')
