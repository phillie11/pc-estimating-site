from django.contrib import admin

from .models import Project, ProjectInvite, ProjectMember


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ['name', 'organization', 'stage', 'value', 'updated_at']
    list_filter = ['organization', 'stage']
    search_fields = ['name', 'client_name']


admin.site.register(ProjectMember)
admin.site.register(ProjectInvite)
