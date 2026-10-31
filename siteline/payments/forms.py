from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone

from core.forms import RecordFormMixin
from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets

from . import rules
from .models import Application, ApplicationLine, Payment

EVIDENCE_ACCEPT = '.pdf,.eml,.msg,image/*'


class ApplicationForm(RecordFormMixin, StyledModelForm):
    class Meta:
        model = Application
        fields = ['valuation_date', 'work_done', 'materials_on_site', 'materials_off_site', 'notes']
        widgets = {'valuation_date': forms.DateInput(attrs={'type': 'date'}),
                   'notes': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, project=None, words=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        for name in ('work_done', 'materials_on_site', 'materials_off_site'):
            self.fields[name].widget.attrs.setdefault('inputmode', 'decimal')
        if words:
            self.fields['valuation_date'].label = words['valuation_date'].capitalize()

    def clean_valuation_date(self):
        day = self.cleaned_data['valuation_date']
        others = Application.objects.filter(project=self.project, valuation_date=day) \
            .exclude(status=Application.STATUS_WITHDRAWN)
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        clash = others.first()
        if clash:
            raise forms.ValidationError(f'{clash.reference} is already made up to {day:%d %b %Y}. Edit that one, or '
                                        'withdraw it first.')
        later = Application.objects.filter(project=self.project, status__in=Application.SHARED_STATUSES,
                                           valuation_date__gt=day).order_by('-valuation_date').first()
        if later:
            raise forms.ValidationError(f'{later.reference} was submitted up to {later.valuation_date:%d %b %Y}, '
                                        'which is after this date. A new application has to be later.')
        return day


class LineForm(StyledModelForm):
    class Meta:
        model = ApplicationLine
        fields = ['section', 'value']
        labels = {'section': 'Section', 'value': 'Value to date'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['value'].widget.attrs.setdefault('inputmode', 'decimal')
        self.fields['section'].widget.attrs.setdefault('placeholder', 'e.g. Strip out, Drylining, Joinery')


LineFormSet = inlineformset_factory(Application, ApplicationLine, form=LineForm, extra=3, can_delete=True,
                                    max_num=60, validate_max=True)


class NoticeForm(forms.Form):
    """A payment notice or a pay less notice: the sum the payer considers due and the basis of it."""
    amount = forms.DecimalField(label='Sum you consider due (excluding VAT)', max_digits=14, decimal_places=2,
                                min_value=Decimal('0'))
    basis = forms.CharField(label='How you worked it out', widget=forms.Textarea(attrs={'rows': 4}),
                            help_text='The basis on which the sum is calculated, as the Construction Act requires: '
                                      'for example, which items you value differently and why.')
    given_on = forms.DateField(label='Date the client gave the notice', required=False,
                               widget=forms.DateInput(attrs={'type': 'date'}))
    note = forms.CharField(label='How the client gave it', required=False,
                           help_text='For example: email from J. Smith, 3 Nov.')
    evidence = forms.FileField(label="The client's notice", required=False,
                               widget=forms.ClearableFileInput(attrs={'accept': EVIDENCE_ACCEPT}),
                               help_text='Their email (saved as PDF or .eml) or letter.')

    def __init__(self, *args, application=None, kind='payment_notice', on_behalf=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.application, self.kind, self.on_behalf = application, kind, on_behalf
        self.fields['amount'].widget.attrs.setdefault('inputmode', 'decimal')
        if kind == 'pay_less':
            self.fields['amount'].label = 'Sum you now consider due (excluding VAT)'
        if not on_behalf:
            for name in ('given_on', 'note', 'evidence'):
                del self.fields[name]
        else:
            self.fields['given_on'].required = True
            self.fields['note'].required = True
            self.fields['amount'].label = 'Sum the client considers due (excluding VAT)'
            self.fields['basis'].label = 'The basis the client gave'

    def clean_evidence(self):
        f = self.cleaned_data.get('evidence')
        if f:
            validate_upload(f)
        return f

    def clean(self):
        data = super().clean()
        app = self.application
        today = timezone.localdate()
        given = data.get('given_on') or today
        if self.on_behalf:
            if not data.get('evidence') and 'evidence' not in self.errors:
                self.add_error('evidence', "Attach the client's email or letter giving this notice.")
            if data.get('given_on'):
                if given > today:
                    self.add_error('given_on', 'This date is in the future.')
                elif app.submitted_at and given < timezone.localdate(app.submitted_at):
                    self.add_error('given_on', 'This is before the application was submitted.')
        if self.kind == 'pay_less' and app is not None:
            refusal = rules.pay_less_refusal(given, app.pay_less_by)
            if refusal:
                raise forms.ValidationError(refusal, code='late')
        data['given_on'] = given
        return data


class ReversalForm(forms.Form):
    """Reverse a payment recorded by mistake (owners and admins). It stays on record, struck through."""
    reason = forms.CharField(label='Why it is being reversed', max_length=250,
                             help_text='For example: recorded against the wrong application, or the amount was wrong.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)


class PaymentForm(StyledModelForm):
    class Meta:
        model = Payment
        fields = ['amount', 'received_on', 'reference']
        widgets = {'received_on': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, application=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = application
        self.fields['amount'].widget.attrs.setdefault('inputmode', 'decimal')
        self.fields['amount'].label = 'Amount received (including VAT)'
        self.initial.setdefault('received_on', timezone.localdate())

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        app = self.application
        if app is not None:
            app._paid_total = None
            left = app.outstanding
            if amount > left:
                symbol = getattr(app.organization, 'currency_symbol', '£') or '£'
                raise forms.ValidationError(
                    f'That is more than the {symbol}{left:,} outstanding (the notified sum plus VAT, less what has '
                    'been received). Overpayments are not supported: check the amount, or record only what is due.')
        return amount

    def clean_received_on(self):
        day = self.cleaned_data['received_on']
        if day > timezone.localdate():
            raise forms.ValidationError('This date is in the future.')
        return day
