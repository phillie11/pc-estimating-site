from django.contrib import admin
from django.db import models

from .context import unscoped
from .models import ActivityEvent, Membership, Organization, TeamInvite


class TenantModelAdmin(admin.ModelAdmin):
    """
    Platform admin sees every company, so its views run inside unscoped():
    the changelist, the FK dropdowns on the change form and the model form's
    unique checks all use TenantModel.objects, which otherwise fails closed
    with no organisation. The page is rendered inside the block too.
    """

    def _unscoped(self, view, *args, **kwargs):
        with unscoped():
            response = view(*args, **kwargs)
            if hasattr(response, 'render') and not getattr(response, 'is_rendered', True):
                response.render()
            return response

    def changelist_view(self, *args, **kwargs):
        return self._unscoped(super().changelist_view, *args, **kwargs)

    def changeform_view(self, *args, **kwargs):
        return self._unscoped(super().changeform_view, *args, **kwargs)

    def delete_view(self, *args, **kwargs):
        return self._unscoped(super().delete_view, *args, **kwargs)

    def history_view(self, *args, **kwargs):
        return self._unscoped(super().history_view, *args, **kwargs)


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    raw_id_fields = ['user']


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'plan', 'subscription_status', 'trial_ends_at', 'current_period_end',
                    'is_suspended', 'created_at']
    list_filter = ['plan', 'subscription_status', 'is_suspended']
    search_fields = ['name', 'slug', 'stripe_customer_id']
    readonly_fields = ['created_at', 'project_counter']
    inlines = [MembershipInline]
    # Django 6.0's default; 5.2 warns unless the scheme is explicit
    formfield_overrides = {models.URLField: {'assume_scheme': 'https'}}


@admin.register(TeamInvite)
class TeamInviteAdmin(admin.ModelAdmin):
    list_display = ['email', 'organization', 'role', 'status', 'created_at']
    list_filter = ['status']


@admin.register(ActivityEvent)
class ActivityEventAdmin(TenantModelAdmin):
    list_display = ['created_at', 'organization', 'actor', 'verb', 'target']
    list_filter = ['organization']

    def get_queryset(self, request):
        return ActivityEvent.all_objects.select_related('organization', 'actor')
