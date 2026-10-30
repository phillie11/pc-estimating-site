from django.contrib import admin

from tenancy.admin import TenantModelAdmin

from .models import Activity, Project, ProjectInvite, ProjectMember


@admin.register(Project)
class ProjectAdmin(TenantModelAdmin):
    list_display = ['name', 'organization', 'stage', 'value', 'updated_at', 'archived_at']
    list_filter = ['organization', 'stage', 'archived_at']
    search_fields = ['name', 'client_name']


admin.site.register(ProjectMember, TenantModelAdmin)
admin.site.register(ProjectInvite, TenantModelAdmin)


@admin.register(Activity)
class ActivityAdmin(TenantModelAdmin):
    list_display = ['name', 'project', 'start', 'finish', 'progress', 'organization']
    list_filter = ['organization']
    search_fields = ['name', 'project__name']
