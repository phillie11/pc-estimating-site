from django import forms

from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets

from . import reader
from .models import OpenItem, PackClarification, PricingSettings


class UploadForm(forms.Form):
    file = forms.FileField(label='Pricing workbook (.xlsx or .xlsm)',
                           help_text=f'The completed internal pricing workbook, up to {reader.MAX_BYTES // 2 ** 20} MB. '
                                     'Each upload is a new revision; earlier ones are kept.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # No accept filter: iPhone Safari won't open the picker at all when the list names a type it doesn't know
        # (the macro-enabled workbook type), and it greys out .xlsm with an .xlsx-only list. The reader refuses
        # anything that isn't a workbook, with a message.
        style_widgets(self)

    def clean_file(self):
        f = self.cleaned_data['file']
        validate_upload(f)
        try:
            reader.check_name(f.name)
        except reader.WorkbookRefused as e:
            raise forms.ValidationError(str(e))
        if f.size > reader.MAX_BYTES:
            raise forms.ValidationError(f'The file is over {reader.MAX_BYTES // 2 ** 20} MB.')
        return f


class ClarificationForm(StyledModelForm):
    class Meta:
        model = PackClarification
        fields = ['ref', 'trade', 'text', 'response', 'status', 'cost_effect', 'owner_text']
        widgets = {'text': forms.Textarea(attrs={'rows': 3}), 'response': forms.Textarea(attrs={'rows': 2})}


class SiteRulesForm(forms.Form):
    site_rules = forms.CharField(label='Site rules', required=False, max_length=20000,
                                 widget=forms.Textarea(attrs={'rows': 12}),
                                 help_text='Working hours, access, parking, noise and dust limits, permits, the '
                                           'client\'s and landlord\'s rules: what the site team must know.')


class OpenItemForm(StyledModelForm):
    class Meta:
        model = OpenItem
        fields = ['owner', 'due_date', 'status', 'note']
        widgets = {'note': forms.TextInput()}

    def __init__(self, *args, owners=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['owner'].queryset = owners
        self.fields['owner'].required = False
        self.fields['owner'].label_from_instance = lambda u: u.full_name or u.email
        self.fields['owner'].empty_label = 'Nobody yet'
        self.fields['owner'].help_text = 'People who can open this pack.'


class SettingsForm(StyledModelForm):
    class Meta:
        model = PricingSettings
        fields = ['preset', 'margin_tolerance', 'item_due_days', 'assign_to_manager', 'unchecked_issue']

    def clean_margin_tolerance(self):
        v = self.cleaned_data['margin_tolerance']
        if v is not None and not 0 <= v <= 50:
            raise forms.ValidationError('Between 0 and 50 percentage points.')
        return v

    def clean_item_due_days(self):
        v = self.cleaned_data['item_due_days']
        if v is not None and not 1 <= v <= 365:
            raise forms.ValidationError('Between 1 and 365 days.')
        return v
