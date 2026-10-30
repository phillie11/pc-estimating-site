from django import forms


class CodeForm(forms.Form):
    code = forms.CharField(
        label='6-digit code', max_length=12,
        widget=forms.TextInput(attrs={'autocomplete': 'one-time-code', 'inputmode': 'numeric',
                                      'pattern': '[0-9 ]*', 'autofocus': True}))


class VerifyForm(forms.Form):
    """Sign-in step two: a code from the app, or one recovery code."""
    code = forms.CharField(
        label='Code from your authenticator app', max_length=12, required=False,
        widget=forms.TextInput(attrs={'autocomplete': 'one-time-code', 'inputmode': 'numeric',
                                      'pattern': '[0-9 ]*', 'autofocus': True}))
    recovery_code = forms.CharField(
        label='Or a recovery code', max_length=24, required=False,
        help_text='Each recovery code works once. Use one if you no longer have your phone.',
        widget=forms.TextInput(attrs={'autocomplete': 'off', 'autocapitalize': 'characters', 'spellcheck': 'false'}))
    remember = forms.BooleanField(label='Remember this device for 30 days', required=False)

    def clean(self):
        data = super().clean()
        if not data.get('code') and not data.get('recovery_code'):
            raise forms.ValidationError('Enter the code from your authenticator app, or a recovery code.')
        return data


class StepUpForm(forms.Form):
    password = forms.CharField(label='Password', required=False,
                               widget=forms.PasswordInput(attrs={'autocomplete': 'current-password', 'autofocus': True}))
    code = forms.CharField(
        label='Or a code from your authenticator app', max_length=12, required=False,
        widget=forms.TextInput(attrs={'autocomplete': 'one-time-code', 'inputmode': 'numeric', 'pattern': '[0-9 ]*'}))

    def __init__(self, *args, has_two_factor=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not has_two_factor:
            del self.fields['code']

    def clean(self):
        data = super().clean()
        if not data.get('password') and not data.get('code'):
            raise forms.ValidationError('Enter your password' + (' or a code.' if 'code' in self.fields else '.'))
        return data
