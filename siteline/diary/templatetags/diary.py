from django import template
from django.utils import timezone

register = template.Library()


@register.inclusion_tag('diary/_overview.html', takes_context=True)
def diary_overview(context):
    """
    On the project overview: for staff on the job, the "Today on site" button and today's entry at
    a glance; for the client, photos marked "share with client" (read only). Nothing for anyone else.
    """
    from ..access import can_see_shared_photos, can_use_diary
    from ..models import DiaryEntry, SitePhoto
    request, project = context.get('request'), context.get('project')
    out = {'org': getattr(request, 'org', None), 'project': project, 'mode': None}
    if request is None or project is None or getattr(request, 'membership', None) is None:
        return out
    if can_use_diary(request, project):
        today = timezone.localdate()
        out.update(mode='staff', today=today, entry=DiaryEntry.objects.filter(project=project, entry_date=today).first(),
                   photo_count=SitePhoto.objects.filter(project=project).count())
    elif can_see_shared_photos(request):
        photos = list(SitePhoto.objects.filter(project=project, shared_with_client=True)[:12])
        out.update(mode='client', photos=photos)
    return out
