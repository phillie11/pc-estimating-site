from django.contrib import admin

from tenancy.admin import TenantModelAdmin

from .models import ApiAccess, ApiToken


@admin.register(ApiToken)
class ApiTokenAdmin(TenantModelAdmin):
    """Platform support can see and revoke tokens; the secret was never stored, so it can't be shown."""
    list_display = ['name', 'organization', 'prefix', 'created_by', 'created_at', 'last_used_at', 'revoked_at']
    list_filter = ['revoked_at']
    search_fields = ['name', 'prefix', 'organization__name']
    readonly_fields = ['organization', 'prefix', 'secret_hash', 'scopes', 'created_by', 'created_at', 'last_used_at',
                       'revoked_by']
    fields = ['name', 'organization', 'prefix', 'scopes', 'created_by', 'created_at', 'expires_at', 'last_used_at',
              'revoked_at', 'revoked_by', 'secret_hash']

    def has_add_permission(self, request):
        return False  # tokens are made in Settings → API, where the secret is shown once


@admin.register(ApiAccess)
class ApiAccessAdmin(TenantModelAdmin):
    list_display = ['at', 'organization', 'token', 'method', 'path', 'status', 'ip']
    list_filter = ['status']
    readonly_fields = ['organization', 'token', 'at', 'method', 'path', 'status', 'ip']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
