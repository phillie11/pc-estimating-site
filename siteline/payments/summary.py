"""Figures other apps read (contracts.commercial.commercial_position): certified and paid to date."""
from decimal import Decimal

from django.db.models import Sum

ZERO = Decimal('0.00')


def certified_and_paid(project):
    """
    (certified to date excluding VAT, paid to date including VAT). Certified: the notified sums of
    applications with a payment or pay less notice, paid, or standing as notified by default.
    """
    from .models import Application, Payment
    apps = Application.objects.filter(project=project, status__in=Application.COUNTED)
    certified = sum((a.notified_sum for a in apps if a.is_certified), ZERO)
    paid = Payment.objects.filter(application__in=apps).aggregate(s=Sum('amount'))['s'] or ZERO
    return certified, paid
