from django.contrib import admin

from .models import StripeEvent


@admin.register(StripeEvent)
class StripeEventAdmin(admin.ModelAdmin):
    list_display = ['received_at', 'event_type', 'event_id', 'organization']
    list_filter = ['event_type']
