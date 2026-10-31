import secrets
from decimal import Decimal

from django import forms

from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets

from .models import ActualCost, Commitment, CostCode, CostSettings
from .money import pennies


class MoneyField(forms.CharField):
    """An amount typed as people type it (1,250.50 or £1250.505), kept as Decimal rounded half up to 1p."""

    def __init__(self, *args, allow_negative=False, **kwargs):
        self.allow_negative = allow_negative
        kwargs.setdefault('max_length', 20)
        super().__init__(*args, **kwargs)
        self.validators = []  # the length check is for text; to_python returns a Decimal and checks its size
        self.widget.attrs.update({'inputmode': 'decimal', 'autocomplete': 'off', 'class': 'mono'})

    def to_python(self, value):
        value = super().to_python(value)
        if value in self.empty_values:
            return None
        try:
            amount = pennies(value)
        except (TypeError, ValueError):
            raise forms.ValidationError('Enter an amount in pounds, e.g. 1250.50.')
        if abs(amount) >= Decimal('1000000000000'):
            raise forms.ValidationError('That amount is too large.')
        if amount < 0 and not self.allow_negative:
            raise forms.ValidationError('Enter an amount of zero or more.')
        return amount


def _codes():
    return CostCode.objects.filter(is_active=True).order_by('code')


class CodeChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f'{obj.code} {obj.name}'


class StyledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)


class OneTimeFormMixin(forms.Form):
    """A fresh hidden key each time the form is shown, so sending the same page twice records it once."""
    form_key = forms.CharField(widget=forms.HiddenInput, required=False, max_length=60)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial['form_key'] = secrets.token_urlsafe(24)


# --- Settings ---------------------------------------------------------------------------

class CostCodeForm(StyledModelForm):
    class Meta:
        model = CostCode
        fields = ['code', 'name', 'group', 'is_active']
        labels = {'group': 'Group (optional)'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A code with money on a job keeps its number and group, so past reports still mean the same thing
        self.locked = bool(self.instance.pk) and self.instance.has_money
        if self.locked:
            for name in ('code', 'group'):
                self.fields[name].widget.attrs.update({'readonly': True, 'aria-readonly': 'true'})
                self.fields[name].help_text = 'Fixed: this code has money on a job.'
            if isinstance(self.fields['group'].widget, forms.Select):
                self.fields['group'].widget.attrs['disabled'] = True
                self.fields['group'].required = False
            self.fields['name'].help_text = 'Correct a spelling or wording only: past jobs show the new name.'

    def clean(self):
        data = super().clean()
        if self.locked:
            for name in ('code', 'group'):
                sent = (self.data.get(self.add_prefix(name)) or '').strip()
                if name == 'group' and name not in self.data:
                    continue  # a disabled select isn't posted
                if sent != (getattr(self.instance, name) or ''):
                    self.add_error(name, 'This code has money on a job, so its code and group can\'t change. '
                                         'Retire it and add a new code instead.')
            data['group'] = self.instance.group
        return data

    def clean_code(self):
        code = self.cleaned_data['code'].strip()
        clash = CostCode.objects.filter(code__iexact=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError('Another cost code already uses this code.')
        return code


class CostSettingsForm(StyledModelForm):
    class Meta:
        model = CostSettings
        fields = ['margin_threshold']

    def clean_margin_threshold(self):
        v = self.cleaned_data['margin_threshold']
        if v is None or v < 0 or v > 100:
            raise forms.ValidationError('Enter a percentage between 0 and 100.')
        return v


# --- Budget -----------------------------------------------------------------------------

class BudgetSetupForm(StyledForm):
    """The first budget, from the tender: value less margin, spread over the cost codes filled in."""
    basis_value = MoneyField(label='Tender or contract value',
                             help_text='Starts from the job\'s contract sum. Change it if the tender was different.')
    margin_pct = forms.DecimalField(label='Margin %', max_digits=5, decimal_places=2, min_value=Decimal('-100'),
                                    max_value=Decimal('100'), help_text='What the tender allowed for overheads and profit.')
    reason = forms.CharField(label='Notes (optional)', required=False, max_length=500,
                             widget=forms.TextInput(attrs={'placeholder': 'e.g. from tender build-up rev C'}))

    def __init__(self, *args, codes=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.codes = list(codes)
        for c in self.codes:
            self.fields[f'amt_{c.pk}'] = MoneyField(label=f'{c.code} {c.name}', required=False)
            self.fields[f'desc_{c.pk}'] = forms.CharField(label='Description', required=False, max_length=200)
            self.fields[f'desc_{c.pk}'].widget.attrs['placeholder'] = c.name

    def rows(self):
        return [(c, self[f'amt_{c.pk}'], self[f'desc_{c.pk}']) for c in self.codes]

    def target(self):
        from .money import budget_from_tender
        d = self.cleaned_data
        return budget_from_tender(d['basis_value'], d['margin_pct'])

    def lines(self):
        return [(c, self.cleaned_data.get(f'desc_{c.pk}', ''), self.cleaned_data[f'amt_{c.pk}'])
                for c in self.codes if self.cleaned_data.get(f'amt_{c.pk}')]

    def clean(self):
        data = super().clean()
        if self.errors or data.get('basis_value') is None or data.get('margin_pct') is None:
            return data
        lines = self.lines()
        if not lines:
            raise forms.ValidationError('Put an amount against at least one cost code.')
        total = sum((a for _, _, a in lines), Decimal('0'))
        target = self.target()
        if total != target:
            diff = target - total
            raise forms.ValidationError(
                f'The lines add up to £{total:,.2f}, but the budget should be £{target:,.2f} '
                f'(£{data["basis_value"]:,.2f} less {data["margin_pct"]}% margin). '
                f'{"Add" if diff > 0 else "Take off"} £{abs(diff):,.2f}, or change the margin.')
        return data


class TransferForm(StyledForm):
    from_code = CodeChoice(queryset=_codes(), label='Move budget from')
    to_code = CodeChoice(queryset=_codes(), label='To')
    amount = MoneyField(label='Amount')
    reason = forms.CharField(max_length=500, label='Reason',
                             widget=forms.TextInput(attrs={'placeholder': 'e.g. ceilings now in the drylining package'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['from_code'].queryset = _codes()
        self.fields['to_code'].queryset = _codes()

    def clean(self):
        data = super().clean()
        if data.get('from_code') and data.get('from_code') == data.get('to_code'):
            raise forms.ValidationError('Pick two different cost codes.')
        if data.get('amount') is not None and data['amount'] <= 0:
            self.add_error('amount', 'Transfer an amount above zero.')
        return data


class RevisionForm(StyledForm):
    cost_code = CodeChoice(queryset=_codes(), label='Cost code')
    amount = MoneyField(label='Change to the budget', allow_negative=True,
                        help_text='Negative to reduce it, e.g. -2500.')
    reason = forms.CharField(max_length=500, label='Reason',
                             widget=forms.TextInput(attrs={'placeholder': 'e.g. VO-002 approved: extra joinery'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['cost_code'].queryset = _codes()

    def clean_amount(self):
        v = self.cleaned_data['amount']
        if v == 0:
            raise forms.ValidationError('Enter an amount other than zero.')
        return v


# --- Commitments ------------------------------------------------------------------------

class OrderForm(OneTimeFormMixin, StyledForm):
    cost_code = CodeChoice(queryset=_codes(), label='Cost code')
    supplier = forms.CharField(max_length=200, label='Supplier')
    value = MoneyField(label='Order value (excluding VAT)')
    reference = forms.CharField(max_length=60, required=False, label='Order number or their quote reference')
    description = forms.CharField(max_length=300, required=False, label='What for',
                                  widget=forms.TextInput(attrs={'placeholder': 'e.g. tower scaffold hire, 6 weeks'}))
    order_date = forms.DateField(required=False, label='Order date')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['cost_code'].queryset = _codes()

    def clean_value(self):
        v = self.cleaned_data['value']
        if v is not None and v <= 0:
            raise forms.ValidationError('Enter a value above zero.')
        return v


class AdjustmentForm(OneTimeFormMixin, StyledForm):
    amount = MoneyField(label='Change to the order', allow_negative=True, help_text='Negative to reduce it.')
    reason = forms.CharField(max_length=300, label='Reason',
                             widget=forms.TextInput(attrs={'placeholder': 'e.g. extra 20m of partition, instruction 14'}))

    def clean_amount(self):
        v = self.cleaned_data['amount']
        if v == 0:
            raise forms.ValidationError('Enter an amount other than zero.')
        return v


# --- Actuals and forecasts --------------------------------------------------------------

class ActualForm(OneTimeFormMixin, StyledForm):
    kind = forms.ChoiceField(choices=ActualCost.KIND_CHOICES, label='Type')
    cost_code = CodeChoice(queryset=_codes(), label='Cost code')
    supplier = forms.CharField(max_length=200, label='Supplier, or who worked')
    reference = forms.CharField(max_length=60, required=False, label='Invoice number or week')
    cost_date = forms.DateField(label='Date')
    amount = MoneyField(label='Amount (excluding VAT)', allow_negative=True, help_text='Negative for a credit note.')
    description = forms.CharField(max_length=300, required=False, label='What for')
    commitment = forms.ModelChoiceField(queryset=Commitment.objects.none(), required=False, label='Against order',
                                        empty_label='No order')
    file = forms.FileField(required=False, label='Invoice or timesheet (optional)')

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['cost_code'].queryset = _codes()
        self.fields['commitment'].queryset = Commitment.objects.filter(project=project).order_by('number')
        self.fields['commitment'].label_from_instance = lambda c: f'{c.ref} {c.supplier}'
        self.fields['file'].widget.attrs['accept'] = '.pdf,image/*,.xlsx,.xls,.csv,.doc,.docx'

    def clean_file(self):
        f = self.cleaned_data.get('file')
        if f and hasattr(f, 'size'):
            validate_upload(f)
        return f

    def clean(self):
        data = super().clean()
        if data.get('amount') == 0:
            self.add_error('amount', 'Enter an amount other than zero.')
        c = data.get('commitment')
        if c and data.get('cost_code') and c.cost_code_id != data['cost_code'].pk:
            self.add_error('cost_code', f'{c.ref} is on {c.cost_code}. Use that code or pick no order.')
        return data


class ForecastForm(StyledForm):
    cost_code = CodeChoice(queryset=CostCode.objects.all(), widget=forms.HiddenInput)
    amount = MoneyField(label='Forecast final cost', required=False,
                        help_text='Leave blank to go back to the standard forecast.')
    reason = forms.CharField(max_length=300, label='Reason',
                             widget=forms.TextInput(attrs={'placeholder': 'e.g. final account agreed with Lund'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['cost_code'].queryset = CostCode.objects.all()


class ReverseForm(StyledForm):
    reason = forms.CharField(max_length=200, label='Why it is being reversed',
                             widget=forms.TextInput(attrs={'placeholder': 'e.g. posted to the wrong cost code'}))

