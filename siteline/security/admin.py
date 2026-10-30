from django.contrib import admin

from .models import RecoveryCode, TwoFactorDevice


@admin.register(TwoFactorDevice)
class TwoFactorDeviceAdmin(admin.ModelAdmin):
    """Platform support resets someone's two-factor sign-in by deleting their device here."""
    list_display = ['user', 'confirmed_at', 'last_used_at', 'created_at']
    search_fields = ['user__email', 'user__full_name']
    fields = ['user', 'confirmed_at', 'last_used_at', 'created_at']  # never the secret
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        RecoveryCode.objects.filter(user=obj.user).delete()
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        RecoveryCode.objects.filter(user__in=queryset.values('user')).delete()
        super().delete_queryset(request, queryset)
