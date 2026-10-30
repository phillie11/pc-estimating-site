from django import template

register = template.Library()


@register.simple_tag
def digest_setting(user):
    """{% digest_setting request.user as pref %}: pref.current and pref.choices for the round-up email."""
    from ..models import DigestPreference
    return {'current': DigestPreference.frequency_for(user), 'choices': DigestPreference.CHOICES}
