import re

from django import forms

from .models import Membership, Organization, TeamInvite


class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ['name', 'logo', 'brand_colour', 'address', 'phone', 'website',
                  'company_number', 'vat_number', 'currency_symbol']
        widgets = {'address': forms.Textarea(attrs={'rows': 3}),
                   'brand_colour': forms.TextInput(attrs={'type': 'color'})}

    def clean_brand_colour(self):
        c = self.cleaned_data['brand_colour']
        if not re.fullmatch(r'#[0-9A-Fa-f]{6}', c or ''):
            raise forms.ValidationError('Use a hex colour like #E8590C')
        return c

    def clean_logo(self):
        logo = self.cleaned_data.get('logo')
        if logo and hasattr(logo, 'size') and logo.size > 2 * 1024 * 1024:
            raise forms.ValidationError('Logo must be under 2 MB.')
        return logo


class TeamInviteForm(forms.ModelForm):
    class Meta:
        model = TeamInvite
        fields = ['email', 'role', 'area']
        help_texts = {'area': 'Estimating sees bids, sub tenders and analytics; projects sees won and live jobs. '
                              'Admins always see both.'}

    def __init__(self, *args, org=None, **kwargs):
        self.org = org
        super().__init__(*args, **kwargs)
        self.fields['area'].required = False

    def clean_area(self):
        return self.cleaned_data.get('area') or Membership.AREA_BOTH

    def clean_email(self):
        email = self.cleaned_data['email'].lower()
        if Membership.objects.filter(organization=self.org, user__email__iexact=email, is_active=True) \
                .exclude(role=Membership.ROLE_EXTERNAL).exists():
            raise forms.ValidationError('This person is already on the team.')
        if TeamInvite.objects.filter(organization=self.org, email__iexact=email, status=TeamInvite.STATUS_PENDING).exists():
            raise forms.ValidationError('An invite is already pending for this email.')
        return email


class MemberRoleForm(forms.Form):
    role = forms.ChoiceField(choices=[c for c in Membership.ROLE_CHOICES if c[0] != Membership.ROLE_EXTERNAL])


class MemberAreaForm(forms.Form):
    area = forms.ChoiceField(choices=Membership.AREA_CHOICES)
