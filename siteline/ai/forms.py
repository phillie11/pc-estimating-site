from decimal import Decimal

from django import forms

from projects.forms import StyledModelForm, style_widgets

from .models import AISettings, BidBrief, BriefItem
from .registry import PLAYBOOK_MAX_CHARS

MAX_CAP = Decimal('1000')


class SettingsForm(StyledModelForm):
    class Meta:
        model = AISettings
        fields = ['enabled', 'monthly_cap', 'who', 'profile', 'overheads_profit']
        widgets = {'profile': forms.Textarea(attrs={'rows': 3})}

    def clean_monthly_cap(self):
        v = self.cleaned_data['monthly_cap']
        if v is None or v < 0 or v > MAX_CAP:
            raise forms.ValidationError(f'Between £0 and £{MAX_CAP:,.0f} a month.')
        return v

    def clean_overheads_profit(self):
        v = self.cleaned_data['overheads_profit']
        if v is not None and not Decimal('0') <= v <= Decimal('100'):
            raise forms.ValidationError('A percentage between 0 and 100.')
        return v


class PlaybookForm(forms.Form):
    text = forms.CharField(
        label='Your playbook', required=False, max_length=PLAYBOOK_MAX_CHARS,
        widget=forms.Textarea(attrs={'rows': 22}),
        help_text=f'Plain text, up to {PLAYBOOK_MAX_CHARS:,} characters. Leave it empty to use Siteline\'s default.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)


class ReadForm(forms.Form):
    revisions = forms.TypedMultipleChoiceField(coerce=int, label='Documents to read',
                                               widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, revisions=(), **kwargs):
        super().__init__(*args, **kwargs)
        from .brief import label
        self.fields['revisions'].choices = [
            (r.pk, f'{label(r)}{f" ({r.page_count} pages)" if r.page_count else ""}') for r in revisions]


class SummaryForm(StyledModelForm):
    class Meta:
        model = BidBrief
        fields = ['recommendation', 'summary', 'value_basis']
        widgets = {'summary': forms.Textarea(attrs={'rows': 4}), 'value_basis': forms.Textarea(attrs={'rows': 2})}


class ItemForm(StyledModelForm):
    class Meta:
        model = BriefItem
        fields = ['section', 'label', 'text', 'date_kind', 'date', 'severity', 'value_low', 'value_high',
                  'source_label', 'source_page']
        widgets = {'text': forms.Textarea(attrs={'rows': 3})}

    def clean_text(self):
        return self.cleaned_data['text'][:2000]
