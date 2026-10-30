from django import forms


class RecordFormMixin:
    """
    For a record's edit ModelForm: posts back the version the form was opened at, so saving
    over someone else's edit is refused with a form error instead of silently overwriting it.
    Put it before the ModelForm base: class RFIForm(RecordFormMixin, StyledModelForm).
    """
    CONFLICT_MESSAGE = 'Someone else changed this since you opened it. Reload the page to see their changes.'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['version'] = forms.IntegerField(widget=forms.HiddenInput, required=False,
                                                    initial=self.instance.version if self.instance.pk else None)

    def clean(self):
        cleaned = super().clean()
        posted = cleaned.get('version')
        if self.instance.pk and posted is not None and posted != self.instance.version:
            raise forms.ValidationError(self.CONFLICT_MESSAGE, code='conflict')
        return cleaned
