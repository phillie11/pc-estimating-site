from django.db import models


class StripeEvent(models.Model):
    """Processed webhook events, so Stripe retries are applied once only."""
    event_id = models.CharField(max_length=80, unique=True)
    event_type = models.CharField(max_length=80)
    organization = models.ForeignKey('tenancy.Organization', null=True, blank=True, on_delete=models.SET_NULL)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f'{self.event_type} {self.event_id}'
