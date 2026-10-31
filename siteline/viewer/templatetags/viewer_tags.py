from django import template
from django.urls import reverse

from viewer.access import can_view_external, is_pdf, pins_for_record

register = template.Library()


@register.filter
def viewable(rev, internal):
    """{% if rev|viewable:is_internal %}: may this revision be opened in the viewer? (It is already visible.)"""
    if not rev:
        return False
    return is_pdf(rev) if internal else can_view_external(rev)


@register.inclusion_tag('viewer/_record_pins.html', takes_context=True)
def record_pins(context, record):
    """The "On drawing X" links on a snag or RFI page, for pins on documents this person can see."""
    request, project = context['request'], context['project']
    links = [(p, reverse('pin_open', args=[request.org.slug, project.pk, p.pk]))
             for p in pins_for_record(request, project, record)]
    return {'links': links}
