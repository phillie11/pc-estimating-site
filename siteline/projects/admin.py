from django.contrib import admin

from .models import Activity, Project, ProjectInvite, ProjectMember


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ['name', 'organization', 'stage', 'value', 'updated_at']
    list_filter = ['organization', 'stage']
    search_fields = ['name', 'client_name']


admin.site.register(ProjectMember)
admin.site.register(ProjectInvite)


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ['name', 'project', 'start', 'finish', 'progress', 'organization']
    list_filter = ['organization']
    search_fields = ['name', 'project__name']
