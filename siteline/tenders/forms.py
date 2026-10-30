from django import forms
from django.utils import timezone

from directory.models import Package, Subcontractor, Trade
from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets

from .models import Addendum, TenderQuery


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


class AddendumForm(StyledModelForm):
    files = MultipleFileField(required=False, help_text='Revised drawings, specs or schedules. Several at once is fine.')

    class Meta:
        model = Addendum
        fields = ['title', 'description']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}
