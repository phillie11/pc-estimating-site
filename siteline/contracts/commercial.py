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
        from payments.access import manages_payments
        ctx['can_see_applications'] = manages_payments(request, project)
        if ctx['can_see_applications']:  # owners, admins and the job's PM only (payments/access.py)
            ctx['commercial_nav'].append(('applications', 'Applications', reverse('application_list', args=[org, pk])))
        from costs.access import can_view_costs  # Costs: the job's PM or QS, owners and admins only
        if can_view_costs(request, project):
            ctx['commercial_nav'].append(('costs', 'Costs', reverse('cost_report', args=[org, pk])))
        ctx['commercial_sub'] = sub
    return ctx


def commercial_position(project):
    """
    Contract sum, approved and pending client variations, forecast final account, and certified
    and paid to date from applications for payment (payments.summary). Staff only.
    """
    from variations.models import Variation
    vos = Variation.objects.filter(project=project)
    approved = vos.filter(status=Variation.STATUS_APPROVED).aggregate(s=Sum('approved_value'))['s'] or Decimal('0')
    pending = vos.filter(status=Variation.STATUS_SUBMITTED).aggregate(s=Sum('value'))['s'] or Decimal('0')
    contract = project.value or Decimal('0')
    from payments.summary import certified_and_paid
    certified, paid = certified_and_paid(project)
    return {'contract': contract, 'approved': approved, 'pending': pending, 'forecast': contract + approved,
            'certified': certified, 'paid': paid}
