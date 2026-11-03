from django import template
from django.utils.safestring import mark_safe

from .. import onboarding, topics

register = template.Library()


@register.simple_tag(takes_context=True)
def help_for(context):
    """The help topic for the page being shown, for this reader, or None (the layout's ? button)."""
    request = context.get('request')
    match = getattr(request, 'resolver_match', None)
    membership = getattr(request, 'membership', None)
    if match is None or membership is None:
        return None
    return topics.for_page(match.url_name, membership.is_internal)


@register.simple_tag(takes_context=True)
def help_body(context, topic, top=3):
    org = context.get('org')
    # Reviewed for B308/B703: help.topics.render_markdown escapes every character of the file before adding
    # its own fixed tags, and only keeps links that start with / or https://.
    return mark_safe(topic.html(org.slug if org else '', top))  # nosec B308 B703


@register.inclusion_tag('help/_onboarding.html', takes_context=True)
def onboarding_checklist(context):
    request = context.get('request')
    return {'checklist': onboarding.checklist(request) if request is not None else None,
            'org': context.get('org'), 'csrf_token': context.get('csrf_token')}
