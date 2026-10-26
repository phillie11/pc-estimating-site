from django import forms

from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets

from .models import Rams, SubVariation


def _checked(f):
    if f and hasattr(f, 'size'):
        validate_upload(f)
    return f


class RamsForm(StyledModelForm):
    class Meta:
        model = Rams
        fields = ['title', 'scope', 'file']
        labels = {'file': 'RAMS document'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['scope'].widget.attrs['placeholder'] = 'e.g. first fix electrical, levels 1–2'

    def clean_file(self):
        return _checked(self.cleaned_data.get('file'))


class ResubmitForm(forms.Form):
    file = forms.FileField(label='Revised RAMS')
    note = forms.CharField(required=False, max_length=300, label='What changed',
                           widget=forms.TextInput(attrs={'placeholder': 'e.g. added hot works permit section'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)

    def clean_file(self):
        return _checked(self.cleaned_data.get('file'))


class RamsReviewForm(forms.Form):
    decision = forms.ChoiceField(choices=Rams.DECISIONS, widget=forms.RadioSelect)
    comments = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)

    def clean(self):
        data = super().clean()
        if data.get('decision') in (Rams.COMMENTS, Rams.REJECTED) and not (data.get('comments') or '').strip():
            self.add_error('comments', 'Say what needs to change.')
        return data


class SubVariationForm(StyledModelForm):
    class Meta:
        model = SubVariation
        fields = ['title', 'reason', 'instructed_by', 'value', 'time_impact_days', 'description', 'file']

    def clean_value(self):
        v = self.cleaned_data['value']
        if v is not None and v <= 0:
            raise forms.ValidationError('Enter the value you are claiming.')
        return v

    def clean_file(self):
        return _checked(self.cleaned_data.get('file'))


class SubVariationDecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=[(SubVariation.APPROVED, 'Approve'), (SubVariation.REJECTED, 'Reject')],
                                 widget=forms.RadioSelect)
    approved_value = forms.DecimalField(required=False, max_digits=14, decimal_places=2,
                                        help_text='Leave as claimed, or enter the value you agree.')
    decision_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}), label='Notes to the subcontractor')
    raise_client_variation = forms.BooleanField(required=False, label='Start a draft client variation for this')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)

    def clean(self):
        data = super().clean()
        if data.get('decision') == SubVariation.REJECTED and not (data.get('decision_notes') or '').strip():
            self.add_error('decision_notes', 'Tell them why it is rejected.')
        v = data.get('approved_value')
        if v is not None and v < 0:
            self.add_error('approved_value', 'Cannot be negative.')
        return data
