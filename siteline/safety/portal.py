"""
Reporting a near miss from a subcontractor's portal (pending owner decision; built as the
recommended default).

The same under-a-minute form the site team uses (what, where, when, a photo, was anyone hurt). It
makes an ordinary incident: internal, with the ball in the project manager's court and due by the
company's investigation target, exactly as a report from our own staff. It records whose company
reported it (Incident.reported_by_company).

  who                        sees
  -------------------------  ---------------------------------------------------------------
  an appointed sub           the form, and the reports their own company made on this job:
                             reference, when, what and whether the site team has closed it.
                             Never the investigation, actions, photos or anyone else's reports.
  staff                      sent to the staff report form
  client, consultant, other  403 (a tendering sub or another company: 404 from get_project)
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from delivery.access import subcontractor_id
from diary import photos as photo_files
from projects.access import get_project, is_internal
from projects.views import project_context
from tenancy import notify
from tenancy.models import log_activity

from . import rules
from .forms import ReportForm
from .models import Incident, IncidentPhoto, SafetySettings

SHOWN = 50


def incident_owner(project):
    """Who investigates: the project manager while they are active staff, else the company's first owner or admin."""
    from tenancy.models import Membership
    staff = Membership.objects.filter(organization_id=project.organization_id, is_active=True, user__is_active=True)
    if project.manager_id and staff.filter(user_id=project.manager_id, role__in=Membership.INTERNAL_ROLES).exists():
        return project.manager
    admin = staff.filter(role__in=Membership.ADMIN_ROLES).select_related('user').order_by('pk').first()
    return admin.user if admin else None


def tab_for(request, project):
    if is_internal(request) or subcontractor_id(request) is None or project.section != 'delivery':
        return None
    return ('nearmiss', 'Report a near miss', reverse('portal_near_miss', args=[request.org.slug, project.pk]), None)


def near_miss(request, org_slug, pk):
    project = get_project(request, pk)
    if is_internal(request):
        return redirect('incident_report', org_slug=org_slug, pk=pk)
    sub = subcontractor_id(request)
    if sub is None or project.section != 'delivery':
        raise PermissionDenied
    form = ReportForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        incident = form.save(commit=False)
        incident.project = project
        incident.kind = Incident.KIND_INJURY if incident.anyone_hurt else Incident.KIND_NEAR_MISS
        incident.reported_by_company_id = sub
        incident.ball_in_court_user = incident_owner(project)
        incident.due_date = rules.investigation_due(timezone.localdate(),
                                                    SafetySettings.for_org(request.org).investigation_days)
        photo = None
        try:
            with transaction.atomic():
                incident.save(actor=request.user)
                f = form.cleaned_data.get('photo')
                if f:
                    photo = IncidentPhoto(incident=incident, uploaded_by=request.user)
                    photo_files.attach(photo, f)
                    photo.save()
        except BaseException:
            if photo is not None:
                photo_files.delete_files(photo)
            raise
        log_activity(request, 'reported', f'{incident.ref} {incident.title}', project=project,
                     url=incident.get_absolute_url(), internal_only=True)
        if incident.ball_in_court_user is not None:
            what = 'Injury' if incident.anyone_hurt else 'Near miss'
            notify.alert([incident.ball_in_court_user], f'{what} reported by {incident.reported_by_company}: '
                         f'{incident.ref} {incident.title}', incident.get_absolute_url(), project=project,
                         actor=request.user, org=request.org)
        messages.success(request, f'Thank you. {incident.ref} has gone to the site team, who will look into it.'
                         + (' If someone is badly hurt, call 999 first.' if incident.anyone_hurt else ''))
        return redirect('portal_near_miss', org_slug=org_slug, pk=pk)
    reports = Incident.objects.filter(project=project, reported_by_company_id=sub).order_by('-occurred_at', '-pk')
    return render(request, 'safety/portal_near_miss.html', project_context(
        request, project, 'nearmiss', form=form, reports=list(reports[:SHOWN])))
