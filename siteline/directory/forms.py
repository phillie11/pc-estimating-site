from django import forms

from projects.forms import StyledModelForm

from .models import Subcontractor, SubcontractorContact, Trade


class SubcontractorForm(StyledModelForm):
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


