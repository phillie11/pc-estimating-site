from django import forms

from core.forms import RecordFormMixin
from directory.models import Package, Trade
from projects.access import staff_users
from projects.forms import StyledModelForm
from rfis.models import RFI

from .models import ChangeEvent, ChangeEventLine, EarlyWarning

NEC_ONLY = ('quotation_instructed_on', 'reply_received_on', 'failure_notified_on')
JCT_ONLY = ('delay_notice_given_on',)


class EarlyWarningForm(RecordFormMixin, StyledModelForm):
    class Meta:
        model = EarlyWarning
        fields = ['title', 'description', 'raised_by_side', 'risk_cost', 'risk_time', 'risk_quality', 'mitigation',
                  'meeting_date', 'ball_in_court_user', 'due_date']
        labels = {'title': 'Early warning', 'ball_in_court_user': 'Owner', 'due_date': 'Review by'}
        help_texts = {'ball_in_court_user': 'Who on our side is dealing with it. It shows in their "Needs me".'}

    def __init__(self, *args, project=None, vocab=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['ball_in_court_user'].queryset = staff_users(project.organization)
        if vocab:
            self.fields['meeting_date'].label = vocab['early_warning_meeting'].capitalize()


class ChangeEventForm(RecordFormMixin, StyledModelForm):
    source_rfi = forms.ModelChoiceField(queryset=RFI.objects.none(), required=False, label='From RFI')
    source_early_warning = forms.ModelChoiceField(queryset=EarlyWarning.objects.none(), required=False,
                                                  label='From early warning')

    class Meta:
        model = ChangeEvent
        fields = ['title', 'origin', 'source_rfi', 'source_early_warning', 'description', 'time_impact_days',
                  'ball_in_court_user', 'aware_on', 'notified_on', 'quotation_instructed_on', 'quotation_submitted_on',
                  'reply_received_on', 'failure_notified_on', 'delay_notice_given_on']
        labels = {'title': 'Change event', 'ball_in_court_user': 'Owner'}
        help_texts = {'ball_in_court_user': 'Who on our side is pricing and chasing it. It shows in their "Needs me".'}

    def __init__(self, *args, project=None, contract=None, vocab=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['ball_in_court_user'].queryset = staff_users(project.organization)
        self.fields['source_rfi'].queryset = RFI.objects.filter(project=project)
        self.fields['source_early_warning'].queryset = EarlyWarning.objects.filter(project=project)
        family = contract.family if contract else None
        drop = NEC_ONLY if family != 'NEC' else JCT_ONLY
        if family not in ('NEC', 'JCT'):
            drop = NEC_ONLY + JCT_ONLY
        for name in drop:
            del self.fields[name]
        if vocab:
            self.fields['notified_on'].label = f"Notified to the {vocab['administrator']} on"
            if 'quotation_instructed_on' in self.fields:
                self.fields['quotation_instructed_on'].label = f"Quotation instructed by the {vocab['administrator']} on"
                self.fields['reply_received_on'].label = f"{vocab['administrator']}'s reply received on"
                self.fields['failure_notified_on'].label = f"Told the {vocab['administrator']} their reply is late on"

    def clean(self):
        data = super().clean()
        if data.get('origin') == 'RFI' and not data.get('source_rfi'):
            self.add_error('source_rfi', 'Pick the RFI this came from, or change the origin.')
        if data.get('origin') == 'EARLY_WARNING' and not data.get('source_early_warning'):
            self.add_error('source_early_warning', 'Pick the early warning this came from, or change the origin.')
        return data


class ChangeEventLineForm(StyledModelForm):
    class Meta:
        model = ChangeEventLine
        fields = ['kind', 'description', 'amount', 'trade', 'package']
        labels = {'kind': 'Cost or revenue', 'package': 'Package (optional)', 'trade': 'Trade (optional)'}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['package'].queryset = Package.objects.filter(project=project).select_related('subcontractor', 'trade')
        self.fields['trade'].queryset = Trade.objects.all()
        self.fields['amount'].widget.attrs.setdefault('inputmode', 'decimal')


class LinkSubVariationForm(forms.Form):
    sub_variation = forms.ModelChoiceField(queryset=None, label='Sub variation')

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        from delivery.models import SubVariation
        self.fields['sub_variation'].queryset = SubVariation.objects.filter(project=project, change_event__isnull=True) \
            .select_related('subcontractor')
