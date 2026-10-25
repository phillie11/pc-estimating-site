from django.contrib import admin

from .models import ActivityEvent, Membership, Organization, TeamInvite


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


@admin.register(TeamInvite)
class TeamInviteAdmin(admin.ModelAdmin):
    list_display = ['email', 'organization', 'role', 'status', 'created_at']
    list_filter = ['status']


@admin.register(ActivityEvent)
class ActivityEventAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'organization', 'actor', 'verb', 'target']
    list_filter = ['organization']

    def get_queryset(self, request):
        return ActivityEvent.all_objects.select_related('organization', 'actor')
