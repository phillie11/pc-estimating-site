"""Figures other apps read (contracts.commercial.commercial_position): certified and paid to date."""
from decimal import Decimal

ZERO = Decimal('0.00')


def certified_and_paid(project):
    """
    (certified to date, paid to date), both excluding VAT (product owner, 31 Oct 2026). Certified:
    the notified sums of applications with a payment or pay less notice, paid, or standing as
    notified by default. Paid: payments received (not reversed), less the VAT in them.
    """
    from .models import Application
    apps = prime_paid_totals(list(Application.objects.filter(project=project, status__in=Application.COUNTED)))
    certified = sum((a.notified_sum for a in apps if a.is_certified), ZERO)
    paid = sum((a.paid_ex_vat for a in apps), ZERO)
    return certified, paid


def prime_paid_totals(apps):
    """
    Fill each application's paid_total (payments received, not reversed) in one query, instead of
    one query per application when a list or the Needs me badge reads them.
    """
    from django.db.models import Sum

    from .models import Payment
    ids = [a.pk for a in apps]
    sums = dict(Payment.objects.filter(application_id__in=ids, reversed_at__isnull=True).values('application_id')
                .annotate(s=Sum('amount')).values_list('application_id', 's')) if ids else {}
    for a in apps:
        a._paid_total = sums.get(a.pk) or ZERO
    return apps
