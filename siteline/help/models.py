from django.conf import settings
from django.db import models

from tenancy.models import TenantModel


class OnboardingState(TenantModel):
    """
    One row per company once someone dismisses the getting-started checklist. With no row the checklist
    shows (to owners and admins) until every step is done. The steps themselves are worked out from the
    company's data each time (help/onboarding.py), so nothing stored here can drift out of step.
    """
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+')

    class Meta(TenantModel.Meta):
        constraints = [models.UniqueConstraint(fields=['organization'], name='help_one_onboarding_per_org')]

    def __str__(self):
        return 'Dismissed' if self.dismissed_at else 'Showing'
