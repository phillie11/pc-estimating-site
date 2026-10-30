from django import forms
from django.utils import timezone

from directory.models import Package, Subcontractor, Trade
from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets

from . import scoring
from .models import Addendum, BidReview, TenderQuery, TenderSettings


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else [data] if data else []
        cleaned = [super(MultipleFileField, self).clean(f, initial) for f in files]
        for f in cleaned:
            validate_upload(f)
        return cleaned


class SubChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, s):
        trades = ', '.join(t.name for t in s.trades.all())
        return f'{s.company_name}{f" ({trades})" if trades else ""}'


class TenderInviteForm(forms.Form):
    trade = forms.ModelChoiceField(queryset=Trade.objects.none())
    subcontractors = SubChoiceField(queryset=Subcontractor.objects.none(), widget=forms.CheckboxSelectMultiple,
                                    help_text='Only subcontractors with an email address in your supply chain are listed.')
    scope = forms.CharField(label='Scope of works', widget=forms.Textarea(attrs={'rows': 5}),
                            help_text='What you want priced. Bidders see this on their tender page.')
    quote_due = forms.DateField(label='Quotes due')

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['trade'].queryset = Trade.objects.all()
        self.fields['subcontractors'].queryset = Subcontractor.objects.exclude(email='') \
            .exclude(status=Subcontractor.STATUS_STRUCK_OFF).filter(archived_at__isnull=True).prefetch_related('trades')
        if project is not None and project.quotes_due:
            self.fields['quote_due'].initial = project.quotes_due
        style_widgets(self)

    def clean_quote_due(self):
        d = self.cleaned_data['quote_due']
        if d < timezone.localdate():
            raise forms.ValidationError('Pick a date that has not passed.')
        return d


class QuoteForm(StyledModelForm):
    class Meta:
        model = Package
        fields = ['quote_value', 'quote_file', 'exclusions', 'valid_until', 'programme_weeks', 'quote_notes']
        labels = {'quote_value': 'Quote value (excl. VAT)', 'quote_file': 'Quote document'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['quote_value'].required = True
        self.fields['quote_file'].help_text = 'Your priced quote or breakdown (PDF, Excel or Word).'
        self.fields['exclusions'].widget.attrs['rows'] = 3
        self.fields['quote_notes'].widget.attrs['rows'] = 2

    def clean_quote_value(self):
        v = self.cleaned_data['quote_value']
        if v is not None and v <= 0:
            raise forms.ValidationError('Enter your quote value.')
        return v

    def clean_quote_file(self):
        f = self.cleaned_data.get('quote_file')
        if f and hasattr(f, 'size'):
            validate_upload(f)
        return f


class DeclineForm(forms.Form):
    reason = forms.CharField(max_length=300, required=False, label='Reason (optional)',
                             widget=forms.TextInput(attrs={'placeholder': 'e.g. no capacity on these dates'}))


class QueryForm(StyledModelForm):
    class Meta:
        model = TenderQuery
        fields = ['question']
        widgets = {'question': forms.Textarea(attrs={'rows': 3})}
        labels = {'question': 'Ask a question'}


class AnswerForm(StyledModelForm):
    class Meta:
        model = TenderQuery
        fields = ['question', 'answer', 'published']
        widgets = {'question': forms.Textarea(attrs={'rows': 2}), 'answer': forms.Textarea(attrs={'rows': 3})}
        help_texts = {'question': 'Edit out anything that identifies who asked before issuing it to everyone.',
                      'published': 'Every tenderer sees the question and answer. Who asked is never shown.'}

    def clean_answer(self):
        a = self.cleaned_data['answer'].strip()
        if not a:
            raise forms.ValidationError('Write the answer.')
        return a


class BidGateForm(StyledModelForm):
    """Intake details and the six bid/no-bid questions (tenders.scoring.QUESTIONS)."""
    class Meta:
        model = BidReview
        fields = ['contract_form', 'amendments', 'bidders', 'client_budget', 'decision_note']
        widgets = {'amendments': forms.Textarea(attrs={'rows': 3}), 'decision_note': forms.Textarea(attrs={'rows': 3})}
        help_texts = {'decision_note': 'Anything the score misses. Required when bidding against a No-go.'}

    def __init__(self, *args, scoring_required=False, suggested=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.scoring_required = scoring_required
        suggested = suggested or {}
        self.fields['contract_form'].choices = [('', 'Not known yet')] + scoring.CONTRACT_FORMS
        self.fields['bidders'].widget.attrs.update({'min': 1, 'max': 50})
        self.question_fields = []
        for q in scoring.QUESTIONS:
            name = f'q_{q.key}'
            current = getattr(self.instance, name)
            self.fields[name] = forms.TypedChoiceField(
                label=q.label, help_text=q.help, required=False, coerce=int, empty_value=None,
                choices=[(str(p), lbl) for p, lbl in q.options], widget=forms.RadioSelect,
                initial=str(current) if current is not None else (str(suggested[q.key]) if q.key in suggested else None))
            self.fields[name].suggested = suggested.get(q.key)
            self.fields[name].weight = q.weight
            self.question_fields.append(name)

    def intake_fields(self):
        return [self[n] for n in ('contract_form', 'amendments', 'bidders', 'client_budget')]

    def questions(self):
        return [self[n] for n in self.question_fields]

    def clean_bidders(self):
        b = self.cleaned_data.get('bidders')
        if b is not None and b < 1:
            raise forms.ValidationError('At least 1 (us).')
        return b

    def clean_client_budget(self):
        v = self.cleaned_data.get('client_budget')
        if v is not None and v < 0:
            raise forms.ValidationError('Enter a positive amount.')
        return v

    def clean(self):
        data = super().clean()
        if self.scoring_required:
            for name in self.question_fields:
                if data.get(name) is None:
                    self.add_error(name, 'Answer this to get a score.')
        return data

    def save(self, commit=True):
        review = super().save(commit=False)
        for name in self.question_fields:
            setattr(review, name, self.cleaned_data.get(name))
        review.rescore()
        if commit:
            review.save()
        return review


class DeclineBidForm(StyledModelForm):
    class Meta:
        model = BidReview
        fields = ['decline_reason', 'decline_detail']
        labels = {'decline_reason': 'Why are we declining?'}
        widgets = {'decline_reason': forms.RadioSelect}
        help_texts = {'decline_detail': 'Required for "Other". Kept with the bid and shown in tender analytics.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['decline_reason'].required = True
        self.fields['decline_reason'].choices = scoring.DECLINE_REASONS

    def clean(self):
        data = super().clean()
        if data.get('decline_reason') == scoring.DECLINE_OTHER and not (data.get('decline_detail') or '').strip():
            self.add_error('decline_detail', 'Say what the reason is.')
        return data


class LossReviewForm(StyledModelForm):
    class Meta:
        model = BidReview
        fields = ['loss_reason', 'our_price', 'winner_price', 'lessons']
        widgets = {'loss_reason': forms.RadioSelect, 'lessons': forms.Textarea(attrs={'rows': 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['loss_reason'].required = True
        self.fields['loss_reason'].choices = scoring.LOSS_REASONS
        self.fields['our_price'].help_text = 'Our tender sum. Filled in from the bid value.'

    def clean(self):
        data = super().clean()
        for f in ('our_price', 'winner_price'):
            if data.get(f) is not None and data[f] <= 0:
                self.add_error(f, 'Enter a positive amount, or leave it blank.')
        if data.get('loss_reason') == scoring.LOSS_OTHER and not (data.get('lessons') or '').strip():
            self.add_error('lessons', 'Say what happened.')
        return data


class TenderSettingsForm(StyledModelForm):
    class Meta:
        model = TenderSettings
        fields = ['gate_threshold', 'min_quotes']

    def clean_gate_threshold(self):
        v = self.cleaned_data['gate_threshold']
        if v < 0:
            raise forms.ValidationError('0 or more.')
        return v

    def clean_min_quotes(self):
        v = self.cleaned_data['min_quotes']
        if not 1 <= v <= 20:
            raise forms.ValidationError('Between 1 and 20.')
        return v


class AddendumForm(StyledModelForm):
    files = MultipleFileField(required=False, help_text='Revised drawings, specs or schedules. Several at once is fine.')

    class Meta:
        model = Addendum
        fields = ['title', 'description']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}
