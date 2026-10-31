from django import forms
from django.db import transaction

from projects.forms import StyledModelForm
from projects.models import ProjectMember

from .models import MainContract
from .presets import uses_valuation_dates

VALUATION_DAY_HELP = (
    'Day of each month for interim valuations: the Interim Valuation Dates in the JCT Contract Particulars, '
    'or the assessment day in the NEC Contract Data. Needed for JCT Intermediate, Standard and Design and '
    'Build, and NEC4, so Siteline can remind you when each application for payment is due. Above 28 means '
    'the last day in shorter months.')
VALUATION_DAY_REQUIRED = (
    'Enter the valuation day. This form of contract pays from a monthly valuation date, and without it '
    'there are no reminders to submit applications for payment. It is in the Contract Particulars '
    '(JCT: Interim Valuation Dates) or the Contract Data (NEC: assessment day).')


class ContractForm(StyledModelForm):
    """
    The contract set-up. The contract sum and the dates are edited here but stored on the
    project (Project.value, start_date, completion_date), which stays their single source.
    """
    contract_sum = forms.DecimalField(label='Contract sum', max_digits=14, decimal_places=2, required=False,
                                      help_text='Kept on the project; the overview and analytics read it from there.')
    start_date = forms.DateField(label='Possession / start on site', required=False)
    completion_date = forms.DateField(label='Completion date', required=False)

    class Meta:
        model = MainContract
        fields = ['form', 'edition', 'nec_main_option', 'contract_sum', 'start_date', 'completion_date',
                  'administrator', 'bank_holiday_region', 'client_response_days', 'lads_rate', 'lads_period', 'retention_percent', 'retention_released_at_pc_percent',
                  'defects_period_months', 'valuation_day', 'due_date_offset_days', 'final_date_offset_days',
                  'pay_less_notice_days', 'payment_notice_days', 'vat_reverse_charge', 'practical_completion_on',
                  'amendments']
        widgets = {'amendments': forms.Textarea(attrs={'rows': 5})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields['administrator'].queryset = ProjectMember.objects.filter(
            project=project, role__in=(ProjectMember.ROLE_CLIENT, ProjectMember.ROLE_CONSULTANT)).select_related('user')
        self.fields['administrator'].label_from_instance = lambda m: f'{m.user} ({m.company_name or m.get_role_display()})'
        self.initial.setdefault('contract_sum', project.value)
        self.initial.setdefault('start_date', project.start_date)
        self.initial.setdefault('completion_date', project.completion_date)
        for name in ('contract_sum', 'lads_rate', 'retention_percent', 'retention_released_at_pc_percent'):
            self.fields[name].widget.attrs.setdefault('inputmode', 'decimal')
        self.fields['valuation_day'].help_text = VALUATION_DAY_HELP
        self.fields['valuation_day'].widget.attrs.setdefault('inputmode', 'numeric')

    def clean(self):
        data = super().clean()
        if data.get('form') != 'NEC4_ECC':
            data['nec_main_option'] = ''
        elif not data.get('nec_main_option'):
            self.add_error('nec_main_option', 'Pick the main option from the Contract Data.')
        if not (data.get('form') or '').startswith('JCT_'):
            data['edition'] = ''
        if uses_valuation_dates(data.get('form')) and not data.get('valuation_day') \
                and 'valuation_day' not in self.errors:
            self.add_error('valuation_day', VALUATION_DAY_REQUIRED)
        start, finish = data.get('start_date'), data.get('completion_date')
        if start and finish and finish < start:
            self.add_error('completion_date', 'Completion is before the start date.')
        return data

    @transaction.atomic
    def save(self, commit=True, actor=None):
        contract = super().save(commit=False)
        contract.project = self.project
        contract.updated_by = actor
        p = self.project
        p.value = self.cleaned_data.get('contract_sum')
        p.start_date = self.cleaned_data.get('start_date')
        p.completion_date = self.cleaned_data.get('completion_date')
        if commit:
            p.save(update_fields=['value', 'start_date', 'completion_date', 'updated_at'])
            contract.save()
        return contract
