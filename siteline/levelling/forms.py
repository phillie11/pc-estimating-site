from decimal import Decimal

from django import forms

from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets

from . import importer
from .models import Adjustment, Exclusion, ScopeLine

MONEY = dict(max_digits=14, decimal_places=2)
LIMIT = Decimal('1000000000')


class PackageChoice(forms.ModelChoiceField):
    def label_from_instance(self, p):
        return p.subcontractor.company_name


def _columns_field(packages, label='Subcontractor'):
    from directory.models import Package
    return PackageChoice(queryset=Package.objects.filter(pk__in=[p.pk for p in packages])
                         .select_related('subcontractor').order_by('subcontractor__company_name'), label=label)


def _bounded(v, field='amount'):
    if v is not None and abs(v) >= LIMIT:
        raise forms.ValidationError('That amount is too large.')
    return v


class LineForm(StyledModelForm):
    class Meta:
        model = ScopeLine
        fields = ['ref', 'description', 'qty', 'unit']
        widgets = {'description': forms.TextInput()}
        help_texts = {'qty': 'Leave blank for a lump-sum item.'}

    def clean_qty(self):
        q = self.cleaned_data.get('qty')
        if q is not None and q < 0:
            raise forms.ValidationError('0 or more.')
        return _bounded(q)


class AdjustmentForm(forms.Form):
    KINDS = [(k, label) for k, label in Adjustment.KIND_CHOICES if k != Adjustment.PLUG]

    package = forms.ModelChoiceField(queryset=None)
    kind = forms.ChoiceField(label='Type', choices=KINDS)
    amount = forms.DecimalField(label='Amount (£)', **MONEY,
                                help_text='Added to their quote. Negative takes money off, e.g. an arithmetic error '
                                          'in our favour.')
    reason = forms.CharField(max_length=500, help_text='What it is for. Printed on the levelling summary.')

    def __init__(self, *args, packages=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['package'] = _columns_field(packages)
        style_widgets(self)

    def clean_amount(self):
        v = self.cleaned_data['amount']
        if v == 0:
            raise forms.ValidationError('Enter an amount other than 0.')
        return _bounded(v)


class ExclusionForm(forms.Form):
    package = forms.ModelChoiceField(queryset=None)
    text = forms.CharField(label='Exclusion or qualification', max_length=500)
    status = forms.ChoiceField(choices=Exclusion.STATUS_CHOICES, initial=Exclusion.OPEN, label='What we have done')
    amount = forms.DecimalField(label='Priced by us (£)', required=False, **MONEY)

    def __init__(self, *args, packages=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['package'] = _columns_field(packages)
        style_widgets(self)

    def clean(self):
        data = super().clean()
        status, amount = data.get('status'), data.get('amount')
        if status == Exclusion.PRICED and amount is None:
            self.add_error('amount', 'Enter what we have allowed for it.')
        if amount is not None:
            try:
                _bounded(amount)
            except forms.ValidationError as e:
                self.add_error('amount', e)
        return data


class ExclusionStatusForm(forms.Form):
    status = forms.ChoiceField(choices=Exclusion.STATUS_CHOICES)
    amount = forms.DecimalField(required=False, **MONEY)

    def clean(self):
        data = super().clean()
        if data.get('status') == Exclusion.PRICED and data.get('amount') is None:
            self.add_error('amount', 'Enter what we have allowed for it.')
        if data.get('amount') is not None and abs(data['amount']) >= LIMIT:
            self.add_error('amount', 'That amount is too large.')
        return data


class CarryForm(forms.Form):
    package = forms.ModelChoiceField(queryset=None, label='Carry in our tender')
    reason = forms.CharField(required=False, max_length=1000, widget=forms.Textarea(attrs={'rows': 2}),
                             help_text='Needed when they are not the lowest levelled price.')

    def __init__(self, *args, packages=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['package'] = _columns_field(packages)
        style_widgets(self)


class ImportForm(forms.Form):
    SCOPE, PRICED = 'scope', 'priced'
    file = forms.FileField(label='Workbook', help_text='Excel (.xlsx) or CSV. Macro-enabled workbooks are refused.',
                           widget=forms.ClearableFileInput(attrs={'accept': '.xlsx,.csv'}))
    kind = forms.ChoiceField(choices=[(SCOPE, 'Our scope lines'), (PRICED, "A subcontractor's priced copy")],
                             widget=forms.RadioSelect, initial=SCOPE, label='What is it?')
    package = forms.ModelChoiceField(queryset=None, required=False, label='Whose priced copy',
                                     help_text='Only for a priced copy: fills that subcontractor\'s column.')

    def __init__(self, *args, packages=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['package'] = _columns_field(packages, "Whose priced copy")
        self.fields['package'].required = False
        self.fields['package'].help_text = 'Only for a priced copy: fills that subcontractor\'s column.'
        style_widgets(self)

    def clean_file(self):
        f = self.cleaned_data['file']
        validate_upload(f)
        try:
            importer.check_name(f.name)
        except importer.WorkbookRefused as e:
            raise forms.ValidationError(str(e))
        return f

    def clean(self):
        data = super().clean()
        if data.get('kind') == self.PRICED and not data.get('package'):
            self.add_error('package', 'Pick whose priced copy this is.')
        return data
