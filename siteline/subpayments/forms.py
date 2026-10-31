from decimal import Decimal

from django import forms
from django.utils import timezone

from costs.forms import MoneyField, OneTimeFormMixin, StyledForm
from projects.forms import StyledModelForm

from . import rules
from .models import Subcontract, SubcontractorTax, SubPaymentSettings

DATE = forms.DateInput(attrs={'type': 'date'})
TERMS_FIELDS = ['cycle_kind', 'valuation_day', 'due_date_offset_days', 'payment_notice_days', 'final_date_offset_days',
                'pay_less_notice_days', 'retention_percent', 'retention_released_at_pc_percent', 'discount_percent']


class TermsMixin:
    def clean(self):
        data = super().clean()
        if data.get('payment_notice_days') is not None and data['payment_notice_days'] > 5:
            self.add_error('payment_notice_days', 'The Construction Act requires the payment notice no later than '
                                                  '5 days after the due date. ' + rules.ADVISORY)
        if data.get('cycle_kind') == rules.CYCLE_MONTHLY and not data.get('valuation_day'):
            self.add_error('valuation_day', 'Give the day of the month for a monthly cycle.')
        fin, pl = data.get('final_date_offset_days'), data.get('pay_less_notice_days')
        if fin is not None and pl is not None and pl >= fin:
            self.add_error('pay_less_notice_days', 'The pay less deadline has to fall after the due date: make this '
                                                   'fewer days than the final date.')
        return data


class SettingsForm(TermsMixin, StyledModelForm):
    class Meta:
        model = SubPaymentSettings
        fields = TERMS_FIELDS


class SubcontractForm(TermsMixin, StyledModelForm):
    dates_text = forms.CharField(label='Valuation dates', required=False, widget=forms.Textarea(attrs={'rows': 3}),
                                 help_text='For a cycle on listed dates: one a line, e.g. 25/11/2026.')

    class Meta:
        model = Subcontract
        fields = TERMS_FIELDS[:2] + ['dates_text'] + TERMS_FIELDS[2:] + ['region', 'practical_completion_on',
                                                                         'defects_period_months']
        widgets = {'practical_completion_on': DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            self.initial['dates_text'] = '\n'.join(f'{d:%d/%m/%Y}' for d in self.instance.listed_dates)

    def clean_dates_text(self):
        try:
            return rules.parse_dates(self.cleaned_data.get('dates_text'))
        except ValueError as e:
            raise forms.ValidationError(str(e))

    def clean(self):
        data = super().clean()
        if data.get('cycle_kind') == rules.CYCLE_DATES and not data.get('dates_text') and 'dates_text' not in self.errors:
            self.add_error('dates_text', 'List at least one valuation date.')
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.valuation_dates = [d.isoformat() for d in self.cleaned_data.get('dates_text') or []]
        if commit:
            obj.save()
        return obj


class TaxForm(StyledModelForm):
    class Meta:
        model = SubcontractorTax
        fields = ['cis_status', 'verification_number', 'verified_on', 'utr', 'vat_rate', 'reverse_charge']
        widgets = {'verified_on': DATE}

    def clean_verified_on(self):
        d = self.cleaned_data.get('verified_on')
        if d and d > timezone.localdate():
            raise forms.ValidationError('This date is in the future.')
        return d

    def clean(self):
        data = super().clean()
        utr = ''.join((data.get('utr') or '').split())
        if utr and not (utr.isdigit() and len(utr) == 10):
            self.add_error('utr', 'A UTR is 10 digits, e.g. 1234567890.')
        data['utr'] = utr
        if data.get('cis_status') in (rules.CIS_GROSS, rules.CIS_REGISTERED) and not data.get('verification_number'):
            self.add_error('verification_number', 'Give the verification number HMRC gave you when you verified them. '
                                                  + rules.HMRC_CHECK)
        return data


class ApplicationForm(OneTimeFormMixin, StyledForm):
    """The sub's application (through the portal), or staff recording one the sub sent by email."""
    valuation_date = forms.DateField(widget=DATE)
    gross_to_date = MoneyField(label='Gross value of work to date (excluding VAT)',
                               help_text='Cumulative, before retention. Approved variations are added for you.')
    materials = MoneyField(label='Cost of materials in this application', required=False,
                           help_text='What the materials cost you, within the sum applied for this time. CIS is not '
                                     'deducted from materials.')
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))
    received_on = forms.DateField(label='Date the application was received', required=False, widget=DATE)
    override_reason = forms.CharField(
        label='Reason, if the gross is over three times the order value', required=False, max_length=250,
        help_text='Only needed when the gross value to date is more than three times the order. It goes in the record.')

    def __init__(self, *args, staff=False, subcontract=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.subcontract, self.staff = subcontract, staff
        if subcontract is not None:
            allowed = subcontract.allowed_dates(timezone.localdate())
            if allowed:
                self.fields['valuation_date'].help_text = 'The subcontract\'s valuation date: ' + ' or '.join(
                    f'{d.day} {d:%b %Y}' for d in reversed(allowed)) + '.'
        if not staff:
            del self.fields['received_on']
            del self.fields['override_reason']
        else:
            self.fields['materials'].label = 'Cost of materials stated by the subcontractor'
            self.fields['materials'].help_text = 'As stated on their application. CIS is not deducted from it.'
            self.initial.setdefault('received_on', timezone.localdate())

    def clean_valuation_date(self):
        from .services import clash
        from .services import date_refusal
        d = self.cleaned_data['valuation_date']
        if self.subcontract is not None:
            problem = date_refusal(self.subcontract, d) or clash(self.subcontract, d)
            if problem:
                raise forms.ValidationError(problem)
        return d

    def clean_received_on(self):
        d = self.cleaned_data.get('received_on') or timezone.localdate()
        if d > timezone.localdate():
            raise forms.ValidationError('This date is in the future.')
        return d

    def clean(self):
        from .services import cap_refusal
        data = super().clean()
        gross = data.get('gross_to_date')
        if self.subcontract is not None and gross is not None:
            over = cap_refusal(self.subcontract, gross)
            if over and not (self.staff and (data.get('override_reason') or '').strip()):
                self.add_error('gross_to_date', over if self.staff else over.replace(
                    'If it is right, staff can record it with a reason.',
                    'If it is right, ask the contractor to record it for you.'))
        return data


class NoticeForm(StyledForm):
    amount = MoneyField(label='Sum we consider due (excluding VAT)')
    basis = forms.CharField(label='How we worked it out', widget=forms.Textarea(attrs={'rows': 4}),
                            help_text='The basis of the sum, as the Construction Act requires: which items we value '
                                      'differently and why.')

    def __init__(self, *args, application=None, kind='payment_notice', **kwargs):
        super().__init__(*args, **kwargs)
        self.application, self.kind = application, kind
        if kind == 'pay_less':
            self.fields['amount'].label = 'Sum we now consider due (excluding VAT)'

    def clean(self):
        data = super().clean()
        if self.kind == 'pay_less' and self.application is not None:
            refusal = rules.pay_less_refusal(timezone.localdate(), self.application.pay_less_by)
            if refusal:
                raise forms.ValidationError(refusal, code='late')
        return data


class MaterialsForm(StyledForm):
    amount = MoneyField(label='Materials for CIS (excluding VAT)')
    reason = forms.CharField(label='Why', max_length=300, help_text='For example: invoices show £3,200 of materials.')

    def __init__(self, *args, application=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = application

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        app = self.application
        if app is not None and amount > max(app.payable, Decimal('0')):
            raise forms.ValidationError(f'Materials can be no more than the sum we pay ({app.payable:,}).')
        return amount


class PaymentForm(OneTimeFormMixin, StyledForm):
    gross = MoneyField(label='Gross paid (excluding VAT, before CIS)')
    materials = MoneyField(label='Of which materials', required=False,
                           help_text='Leave blank to use what is left of the materials on the application.')
    paid_on = forms.DateField(label='Date paid', widget=DATE,
                              help_text='Not before the application was received. A date in an earlier tax month '
                                        'goes on that month\'s CIS figures, whose return may already be filed.')
    reference = forms.CharField(label='Payment reference', required=False, max_length=120)

    def __init__(self, *args, application=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = application
        self.initial.setdefault('paid_on', timezone.localdate())

    def clean_paid_on(self):
        d = self.cleaned_data['paid_on']
        if d > timezone.localdate():
            raise forms.ValidationError('This date is in the future.')
        if self.application is not None and d < self.application.received_on:
            raise forms.ValidationError(f'This is before the application was received '
                                        f'({self.application.received_on:%d %b %Y}).')
        return d

    def clean_gross(self):
        g = self.cleaned_data['gross']
        if g is None or g <= 0:
            raise forms.ValidationError('Enter the gross amount paid.')
        app = self.application
        if app is not None:
            app._paid_gross = None
            if g > app.outstanding:
                raise forms.ValidationError(f'That is more than the {app.outstanding:,} still to pay (excluding VAT, '
                                            'before CIS). Overpayments are not supported.')
        return g

    def clean_materials(self):
        m = self.cleaned_data.get('materials')
        if m is not None and self.application is not None and m > self.application.materials_left:
            raise forms.ValidationError(f'Only {self.application.materials_left:,} of materials is left on this '
                                        'application. Adjust the materials first if that is wrong.')
        return m


class ReversalForm(StyledForm):
    reason = forms.CharField(label='Why it is being reversed', max_length=250,
                             help_text='For example: recorded against the wrong application.')


class ReleaseForm(StyledForm):
    stage_date = forms.DateField(label='Release date', widget=DATE,
                                 help_text='Our practical completion date, or the end of the defects period.')


ZERO = Decimal('0.00')
