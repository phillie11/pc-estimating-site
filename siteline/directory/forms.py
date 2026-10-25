from django import forms

from projects.forms import StyledModelForm

from .models import Package, Subcontractor, SubcontractorContact, Trade


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


class PackageForm(StyledModelForm):
    class Meta:
        model = Package
        fields = ['trade', 'subcontractor', 'status', 'quote_value', 'notes']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['subcontractor'].queryset = Subcontractor.objects.exclude(status=Subcontractor.STATUS_STRUCK_OFF)
        self.fields['trade'].queryset = Trade.objects.all()
