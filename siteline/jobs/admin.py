from django.contrib import admin, messages

import jobs

from . import outbox
from .models import Job, OutboxEmail


class PlatformOnlyAdmin(admin.ModelAdmin):
    """
    Superusers only: jobs and emails hold every company's data (payloads, email bodies),
    so a staff login with model permissions isn't enough. Read only apart from the actions.
    """

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request)


@admin.register(Job)
class JobAdmin(PlatformOnlyAdmin):
    list_display = ['id', 'name', 'status', 'organization', 'attempts', 'max_attempts', 'run_after', 'created_at',
                    'finished_at']
    list_filter = ['status', 'name']
    search_fields = ['name', 'idempotency_key', 'last_error']
    list_select_related = ['organization']
    actions = ['run_again']

    @admin.action(description='Run again now (fresh attempts)')
    def run_again(self, request, queryset):
        n = sum(jobs.retry(job) for job in queryset.exclude(status=Job.RUNNING))
        self.message_user(request, f'{n} job(s) queued to run again.', messages.SUCCESS)


@admin.register(OutboxEmail)
class OutboxEmailAdmin(PlatformOnlyAdmin):
    list_display = ['id', 'subject', 'recipients', 'organization', 'status', 'attempts', 'created_at', 'sent_at']
    list_filter = ['status']
    search_fields = ['subject', 'to', 'last_error']
    list_select_related = ['organization']
    actions = ['send_again']

    @admin.display(description='To')
    def recipients(self, obj):
        return ', '.join(obj.to)

    @admin.action(description='Send again')
    def send_again(self, request, queryset):
        emails = list(queryset.exclude(status=OutboxEmail.SENT))
        for email in emails:
            outbox.resend(email)
        self.message_user(request, f'{len(emails)} email(s) queued to send again (sent ones are skipped).',
                          messages.SUCCESS)
