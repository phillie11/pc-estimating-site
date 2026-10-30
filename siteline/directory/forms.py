from django import forms

from projects.forms import StyledModelForm

from .models import Subcontractor, SubcontractorContact, Trade


class SubcontractorForm(StyledModelForm):
    # Explicit so a bare "example.com" becomes https:// (Django 6.0's default; 5.2 warns without it)
    website = forms.URLField(required=False, max_length=200, assume_scheme='https')

    class Meta:
        model = Subcontractor
        fields = ['company_name', 'trades', 'status', 'contact_name', 'email', 'phone', 'address', 'regions',
                  'website', 'rating', 'public_liability_expiry', 'employers_liability_expiry', 'accreditations', 'notes']
        widgets = {'trades': forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['trades'].queryset = Trade.objects.all()


class ContactForm(StyledModelForm):
    class Meta:
        model = SubcontractorContact
        fields = ['name', 'role', 'email', 'phone']


class TradeForm(forms.ModelForm):
    class Meta:
        model = Trade
        fields = ['name']


