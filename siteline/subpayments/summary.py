"""
What cost control reads from sub payments (costs imports this lazily, only when the app is installed).

    paid_through_subpayments(commitment)  True when the order's package has a subcontract here, so its
                                          costs come from sub payments, never hand-entered actual costs
    payment_ledger_total(commitment)      ACTUAL ledger entries posted by sub payments on the order,
                                          net of reversals (for "Invoiced to date")
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Sum


def paid_through_subpayments(commitment):
    from .models import Subcontract
    return bool(commitment.package_id) and Subcontract.objects.filter(package_id=commitment.package_id).exists()


def payment_ledger_total(commitment):
    from costs.models import LedgerEntry

    from .models import SubPayment
    if not commitment.package_id:
        return Decimal('0.00')
    ids = SubPayment.objects.filter(application__subcontract__package_id=commitment.package_id).values('pk')
    return LedgerEntry.objects.filter(project_id=commitment.project_id, kind=LedgerEntry.ACTUAL,
                                      source_type=ContentType.objects.get_for_model(SubPayment),
                                      source_id__in=ids).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
