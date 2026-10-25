from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm

from .models import User


class LoginForm(AuthenticationForm):
    username = forms.EmailField(label='Email', widget=forms.EmailInput(attrs={'autofocus': True, 'autocomplete': 'email'}))


class _PasswordMixin(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
                               help_text='At least 10 characters.')

    def clean_password(self):
        pw = self.cleaned_data['password']
        password_validation.validate_password(pw)
        return pw


class SignupForm(_PasswordMixin):
    company_name = forms.CharField(max_length=160, label='Company name')
    full_name = forms.CharField(max_length=150, label='Your name')
    email = forms.EmailField(label='Work email')
    field_order = ['company_name', 'full_name', 'email', 'password']

    def clean_email(self):
        email = self.cleaned_data['email'].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists. Log in, then create a new company from the switcher.')
        return email


class AcceptInviteNewUserForm(_PasswordMixin):
    full_name = forms.CharField(max_length=150, label='Your name')
    field_order = ['full_name', 'password']


class NewOrgForm(forms.Form):
    company_name = forms.CharField(max_length=160)


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['full_name', 'job_title', 'phone']
