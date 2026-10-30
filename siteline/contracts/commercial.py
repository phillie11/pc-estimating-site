"""
The project's Commercial tab: one tab with sub-navigation for the contract set-up, the change
register (early warnings and change events) and client variations.

Staff see the Commercial tab. Clients and consultants see only client variations, under their
own Variations tab, as before. Every commercial view builds its context here.
"""
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse

from projects.access import is_internal
from projects.views import project_context

from .vocabulary import vocabulary_for


def commercial_context(request, project, sub, **extra):
    internal = is_internal(request)
    words = vocabulary_for(project)
    ctx = project_context(request, project, 'commercial' if internal else 'variations', vocab=words, **extra)
    if internal:
        org, pk = request.org.slug, project.pk
        ctx['commercial_nav'] = [
            ('contract', 'Contract', reverse('contract_detail', args=[org, pk])),
            ('changes', 'Change register', reverse('change_register', args=[org, pk])),
            ('variations', f"Client {words['change_plural']}", reverse('variation_list', args=[org, pk])),
        ]
        ctx['commercial_sub'] = sub
    return ctx


def commercial_position(project):
    """Contract sum, approved and pending client variations, forecast final account. Staff only."""
    from variations.models import Variation
    vos = Variation.objects.filter(project=project)
    approved = vos.filter(status=Variation.STATUS_APPROVED).aggregate(s=Sum('approved_value'))['s'] or Decimal('0')
    pending = vos.filter(status=Variation.STATUS_SUBMITTED).aggregate(s=Sum('value'))['s'] or Decimal('0')
    contract = project.value or Decimal('0')
    return {'contract': contract, 'approved': approved, 'pending': pending, 'forecast': contract + approved}
