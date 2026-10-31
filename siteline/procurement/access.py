"""
Who sees what in procurement. Every view fetches the project with projects.access.get_project()
first (sections, external roles, archived read only), then asks here.

  who                         schedule   submittals
  --------------------------  ---------  ------------------------------------------------------
  staff on the job            all        all
  subcontractor (appointed)   -          those on their own appointed packages; submit, resubmit
  consultant / designer       -          only revisions forwarded to them; comment on them
  client                      -          nothing (403)
  tendering subcontractor     -          nothing (get_project refuses them)

A subcontractor never sees the designer's comments or mark-ups (the contractor folds them into
its own review), and externals never see a member of staff's name as ball in court.
"""
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from delivery.access import subcontractor_id
from directory.models import Package
from projects.access import is_internal
from projects.models import ProjectMember

STAFF, SUB, CONSULTANT = 'staff', 'sub', 'consultant'


def role(request):
    """STAFF, SUB (an appointed subcontractor on this project), CONSULTANT, or None."""
    if is_internal(request):
        return STAFF
    if subcontractor_id(request) is not None:
        return SUB
    pm = getattr(request, 'project_member', None)
    if pm is not None and pm.role == ProjectMember.ROLE_CONSULTANT:
        return CONSULTANT
    return None


def require_staff(request):
    if not is_internal(request):
        raise PermissionDenied


def submittal_q(who, user, sub_id=None):
    """Q over Submittal for an external person (None for staff: everything)."""
    if who == STAFF:
        return None
    if who == SUB:
        return Q(package__subcontractor_id=sub_id, package__status=Package.APPOINTED)
    if who == CONSULTANT:
        return Q(revisions__forwarded_to=user)
    return Q(pk__in=[])


def submittals_for(request, project):
    """Submittals on this project this person may open; refuses anyone with no procurement access."""
    from .models import Submittal
    who = role(request)
    if who is None:
        raise PermissionDenied
    qs = Submittal.objects.filter(project=project)
    q = submittal_q(who, request.user, subcontractor_id(request))
    if q is not None:
        qs = qs.filter(q).distinct()
    return qs


def revisions_for(request, submittal):
    """Revisions of a submittal this person may see: all of them, or (consultant) those forwarded to them."""
    qs = submittal.revisions.all().prefetch_related('files').select_related(
        'submitted_by', 'reviewed_by', 'forwarded_to', 'forwarded_by')
    if role(request) == CONSULTANT:
        qs = qs.filter(forwarded_to=request.user)
    return qs


def file_visible(request, f):
    """A file on a revision the person can already see: designers' mark-ups stay off the subcontractor's view."""
    from .models import SubmittalFile
    who = role(request)
    if who == SUB:
        return f.role != SubmittalFile.CONSULTANT
    if who == CONSULTANT:
        return f.role != SubmittalFile.MARKUP
    return who == STAFF


def can_submit(request, submittal):
    """Staff (on behalf of a supplier) or the subcontractor whose appointed package it is."""
    from .models import Submittal
    if submittal.status not in Submittal.TO_SUBMIT:
        return False
    who = role(request)
    if who == STAFF:
        return True
    return (who == SUB and submittal.package_id is not None
            and submittal.package.subcontractor_id == subcontractor_id(request)
            and submittal.package.status == Package.APPOINTED)


def appointed_packages(request, project):
    """Packages this subcontractor is appointed on, on this project (none for anyone else)."""
    sub = subcontractor_id(request)
    if sub is None:
        return Package.objects.none()
    return Package.objects.filter(project=project, subcontractor_id=sub, status=Package.APPOINTED)


def consultants(project):
    """Designers and consultants on the job, who a submittal can be forwarded to for comment."""
    from django.contrib.auth import get_user_model
    ids = ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_CONSULTANT).values('user_id')
    return get_user_model().objects.filter(pk__in=ids, is_active=True).order_by('full_name')


def tab_for(request, project):
    """The project tab for this person, or None: staff get Procurement, appointed subs and consultants Submittals."""
    from django.urls import reverse

    from .models import ScheduleLine, Submittal
    who = role(request)
    org, pk = request.org.slug, project.pk
    if who == STAFF:
        from django.utils import timezone
        # The badge: submittals waiting for our review, plus lines already late to order (red)
        waiting = Submittal.objects.filter(project=project, status=Submittal.SUBMITTED).count()
        late = ScheduleLine.objects.filter(project=project, order_by__lt=timezone.localdate()) \
            .exclude(status__in=ScheduleLine.DONE_STATUSES).count()
        return ('procurement', 'Procurement', reverse('procurement_schedule', args=[org, pk]), waiting + late)
    if who == SUB:
        n = submittals_for(request, project).filter(status__in=Submittal.TO_SUBMIT).count()
        return ('submittals', 'Submittals', reverse('submittal_list', args=[org, pk]), n)
    if who == CONSULTANT:
        qs = submittals_for(request, project)
        if not qs.exists():
            return None
        n = qs.filter(status=Submittal.WITH_CONSULTANT, ball_in_court_user=request.user).count()
        return ('submittals', 'Submittals', reverse('submittal_list', args=[org, pk]), n)
    return None
