from django import forms
from django.core.exceptions import ValidationError

from core.forms import RecordFormMixin
from projects.files import validate_upload
from projects.forms import StyledModelForm, style_widgets

from . import rules
from .models import (CommercialSettings, CVRAdjustment, CVRPeriod, DayworkLine, DayworkRate, DayworkSheet,
                     DayworkTerms)

MONEY_ATTRS = {'inputmode': 'decimal', 'autocomplete': 'off'}


class StyledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self)


# --- the CVR -------------------------------------------------------------------------

class StartForm(StyledForm):
    month = forms.ChoiceField(label='Month')

    def __init__(self, *args, months=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['month'].choices = [(m.isoformat(), rules.month_label(m)) for m in months]


class CommentaryForm(RecordFormMixin, StyledModelForm):
    class Meta:
        model = CVRPeriod
        fields = ['commentary']


class AdjustmentForm(StyledModelForm):
    class Meta:
        model = CVRAdjustment
        fields = ['kind', 'amount', 'reason']
        labels = {'amount': 'Amount (£)', 'reason': 'What it is for'}
        help_texts = {'reason': "e.g. Joinery invoice for October not yet received; client's QS valued the bar "
                                'back £4,250 under.'}
        widgets = {'amount': forms.TextInput(attrs=MONEY_ATTRS)}

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount == 0:
            raise ValidationError('Enter an amount other than nil. Use a minus sign to take money off.')
        return amount


class ReverseForm(StyledForm):
    reason = forms.CharField(label='Why it is being reversed', max_length=250)


class SettingsForm(StyledModelForm):
    class Meta:
        model = CommercialSettings
        fields = ['cvr_due_day', 'pm_can_sign_off', 'labour_pct', 'materials_pct', 'plant_pct']
        help_texts = {'labour_pct': 'Starting percentage additions for a new job\'s daywork. Each job can change '
                                    'its own under Dayworks > Rates.'}


# --- dayworks ------------------------------------------------------------------------

class SheetForm(RecordFormMixin, StyledModelForm):
    class Meta:
        model = DayworkSheet
        fields = ['title', 'work_date', 'location', 'instructed_by', 'description']
        labels = {'title': 'What work was done'}
        help_texts = {'title': 'A few words, e.g. "Move bar servery 600mm after client walkround".'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['title'].widget.attrs.setdefault('autocomplete', 'off')


class LineForm(StyledModelForm):
    """One line of labour, plant or materials. A rate from the job's list fills the rate in."""
    rate = forms.ModelChoiceField(queryset=DayworkRate.objects.none(), required=False, label='Rate from the list')

    class Meta:
        model = DayworkLine
        fields = ['kind', 'rate', 'description', 'quantity', 'unit', 'unit_rate']
        labels = {'kind': 'Labour, plant or materials', 'quantity': 'Hours or quantity',
                  'unit': 'Unit (materials)', 'unit_rate': 'Rate (£)'}
        help_texts = {'unit_rate': 'Leave blank to use the rate from the list. Materials: the invoice price each.'}
        widgets = {'quantity': forms.TextInput(attrs={'inputmode': 'decimal'}),
                   'unit_rate': forms.TextInput(attrs=MONEY_ATTRS)}

    def __init__(self, *args, project=None, kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['rate'].queryset = DayworkRate.objects.filter(project=project, is_active=True)
        self.fields['unit_rate'].required = False
        self.fields['description'].required = False
        self.fields['description'].label = 'Who or what'
        self.fields['description'].help_text = 'Name and trade, plant item or material. Optional with a rate chosen.'
        if kind:
            self.fields['kind'].initial = kind

    def clean(self):
        data = super().clean()
        rate, kind = data.get('rate'), data.get('kind')
        if rate is not None and kind and rate.kind != kind:
            self.add_error('rate', f'That rate is for {rate.get_kind_display().lower()}. Choose a '
                                   f'{dict(DayworkLine.KIND_CHOICES).get(kind, "").lower()} rate or none.')
        if data.get('unit_rate') is None:
            if rate is None:
                self.add_error('unit_rate', 'Enter the rate, or choose one from the list.')
            else:
                data['unit_rate'] = rate.rate
        if not (data.get('description') or '').strip():
            if rate is None:
                self.add_error('description', 'Say who or what this is.')
            else:
                data['description'] = rate.name
        if kind in DayworkLine.UNITS and not data.get('unit'):
            data['unit'] = rate.unit + 's' if rate is not None and not rate.unit.endswith('s') else DayworkLine.UNITS[kind]
        return data


class TermsForm(StyledModelForm):
    class Meta:
        model = DayworkTerms
        fields = ['labour_pct', 'materials_pct', 'plant_pct', 'basis']


class RateForm(StyledModelForm):
    class Meta:
        model = DayworkRate
        fields = ['kind', 'name', 'unit', 'rate']
        widgets = {'rate': forms.TextInput(attrs=MONEY_ATTRS)}


def validate_picture(f, label='photo'):
    """Allowed type and size (projects.files), then a picture Pillow can open fully."""
    validate_upload(f)
    from PIL import Image
    try:
        f.seek(0)
        img = Image.open(f)
        if img.format not in ('PNG', 'JPEG', 'MPO', 'WEBP', 'GIF'):
            raise ValidationError(f'The {label} must be a PNG, JPEG, WebP or GIF picture.')
        if img.width * img.height > 40_000_000:
            raise ValidationError(f'The {label} is too large a picture.')
        img.load()
    except ValidationError:
        raise
    except Exception:  # noqa: BLE001 - anything Pillow can't decode is a bad upload, shown as a form error
        raise ValidationError(f'The {label} could not be read as a picture. Try again.')
    finally:
        f.seek(0)
    return img


def is_blank(img):
    """True if the picture has no marks: a signature pad that was never written on."""
    from PIL import Image, ImageOps
    if img.mode in ('RGBA', 'LA', 'PA') or 'transparency' in img.info:
        background = Image.new('RGB', img.size, (255, 255, 255))
        rgba = img.convert('RGBA')
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    grey = ImageOps.invert(img.convert('L'))
    return grey.point(lambda p: 255 if p > 40 else 0).getbbox() is None


class PhotoForm(StyledForm):
    photo = forms.FileField(label='Take or choose a photo',
                            widget=forms.ClearableFileInput(attrs={'accept': 'image/*', 'capture': 'environment'}))
    caption = forms.CharField(max_length=200, required=False, label='Caption (optional)')

    def clean_photo(self):
        f = self.cleaned_data['photo']
        validate_picture(f)
        return f


class SignatureForm(StyledForm):
    name = forms.CharField(label='Name of the person signing', max_length=150)
    role = forms.ChoiceField(label='Signing as', choices=DayworkSheet.SIGNER_ROLES, initial='CLIENT')
    signature = forms.FileField(
        label='Signature', help_text='Sign in the box with a finger. No touch screen? Upload a photo of the signed '
                                     'paper sheet instead.',
        widget=forms.ClearableFileInput(attrs={'accept': 'image/png,image/jpeg', 'class': 'sig-file'}))
    confirm = forms.BooleanField(label='They have read the hours, plant and materials on this sheet', required=True)

    def clean_signature(self):
        f = self.cleaned_data['signature']
        img = validate_picture(f, 'signature')
        if is_blank(img):
            raise ValidationError('The signature box is empty. Ask them to sign in the box, then press Save.')
        return f


class SendForm(StyledForm):
    change_event = forms.ChoiceField(label='Put it on', required=False)

    def __init__(self, *args, change_events=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['change_event'].choices = [('', 'A new change event')] + [
            (str(ce.pk), f'{ce.reference} {ce.title}') for ce in change_events]

