from django.contrib import admin

from .models import RecoveryCode, SecurityEvent, TwoFactorDevice


@admin.register(TwoFactorDevice)
class TwoFactorDeviceAdmin(admin.ModelAdmin):
    """Platform support resets someone's two-factor sign-in by deleting their device here."""
    list_display = ['user', 'confirmed_at', 'last_used_at', 'failed_attempts', 'locked_at']
    list_filter = [('locked_at', admin.EmptyFieldListFilter)]
    search_fields = ['user__email', 'user__full_name']
    fields = ['user', 'confirmed_at', 'last_used_at', 'failed_attempts', 'locked_at', 'created_at']  # never the secret
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        RecoveryCode.objects.filter(user=obj.user).delete()
        SecurityEvent.record(obj.user, SecurityEvent.RESET, actor=request.user, detail='by platform support')
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for device in queryset.select_related('user'):
            SecurityEvent.record(device.user, SecurityEvent.RESET, actor=request.user, detail='by platform support')
        RecoveryCode.objects.filter(user__in=queryset.values('user')).delete()
        super().delete_queryset(request, queryset)


@admin.register(SecurityEvent)
class SecurityEventAdmin(admin.ModelAdmin):
    """Read-only trail of two-factor changes, locks and resets."""
    list_display = ['created_at', 'user', 'kind', 'actor', 'organization', 'detail']
    list_filter = ['kind']
    search_fields = ['user__email', 'actor__email']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
