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
                  'quotation_due', 'due_date',
                  'proceed_at_risk', 'client_reference', 'rfi', 'attachment']
        widgets = {'description': forms.Textarea(attrs={'rows': 6})}
        labels = {'due_date': 'Client to respond by'}
        help_texts = {'due_date': 'Set when it is submitted; change it if you agreed another date with the client.'}

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
            del self.fields['due_date']  # submitting sets it

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
    evidence = forms.FileField(
        label="The client's decision", required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.pdf,.eml,.msg,image/*'}),
        help_text="Recording it for the client: attach their email (saved as PDF or .eml) or letter.")

    def __init__(self, *args, on_behalf=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_behalf = on_behalf
        if not on_behalf:
            del self.fields['evidence']
        else:
            self.fields['decision_notes'].help_text = 'Say how the client gave this decision (e.g. email from J. Smith, 3 Nov).'

    def clean_evidence(self):
        f = self.cleaned_data.get('evidence')
        if f:
            validate_upload(f)
        return f

    def clean(self):
        data = super().clean()
        if self.on_behalf:
            if not (data.get('decision_notes') or '').strip():
                self.add_error('decision_notes', 'Say how the client gave this decision (e.g. email from J. Smith, 3 Nov).')
            if not data.get('evidence') and 'evidence' not in self.errors:
                self.add_error('evidence', "Attach the client's email or letter giving this decision.")
        return data
