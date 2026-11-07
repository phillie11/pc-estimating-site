from datetime import timedelta

from django import forms
from django.utils import timezone

from .models import ALWAYS, DEFAULT_SCOPES, SCOPE_CHOICES

EXPIRY_CHOICES = [('90', 'In 90 days'), ('365', 'In a year'), ('', 'Never (until it is revoked)')]


class TokenForm(forms.Form):
    name = forms.CharField(label='What is it for?', max_length=80,
                           help_text='A name you\'ll recognise, e.g. "Power BI dashboard" or "Finance system".',
                           error_messages={'required': 'Say what the token is for, so you know which one to revoke.'})
    scopes = forms.MultipleChoiceField(
        label='What it can read', choices=[c for c in SCOPE_CHOICES if c[0] not in ALWAYS],
        widget=forms.CheckboxSelectMultiple, required=False, initial=[s for s in DEFAULT_SCOPES if s not in ALWAYS],
        help_text='Projects are always included. Money and incidents are only for owners, admins and each job\'s '
                  'PM or QS on screen, so tick them only if whatever uses the token needs them.')
    expires = forms.ChoiceField(label='Stops working', choices=EXPIRY_CHOICES, initial='365', required=False)

    def clean_name(self):
        name = ' '.join(self.cleaned_data['name'].split())
        if not name:
            raise forms.ValidationError('Say what the token is for, so you know which one to revoke.')
        return name

    def expires_at(self):
        days = self.cleaned_data.get('expires')
        return timezone.now() + timedelta(days=int(days)) if days else None
