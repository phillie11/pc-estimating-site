"""
Who sees what in close-out. Every view fetches the project with projects.access.get_project()
first (sections, external roles, archived read only), then asks here.

  who                        Close-out tab pages                     "Defects and handover" page
  -------------------------  --------------------------------------  ---------------------------------------
  staff on the job           everything; set PC, request O&M,         -
                             compile, build and issue packs
  client, consultant         -                                        PC and defects dates; report a defect;
                                                                      defects reported by the client side;
                                                                      O&M manuals shared with them; issued
                                                                      handover packs and their files
  subcontractor              -  (their O&M requests are submittals,   - (defects assigned to them are on
                             under Decisions -> Submittals)           Snags, as any snag)
  tendering subcontractor    -  (get_project refuses them)            -

No money is shown on any close-out page.
"""
from django.core.exceptions import PermissionDenied

from projects.access import external_role, is_internal
from projects.models import ProjectMember

STAFF, CLIENT = 'staff', 'client'
CLIENT_ROLES = (ProjectMember.ROLE_CLIENT, ProjectMember.ROLE_CONSULTANT)


def role(request):
    if is_internal(request):
        return STAFF
    if external_role(request) in CLIENT_ROLES:
        return CLIENT
    return None


def require_staff(request):
    if not is_internal(request):
        raise PermissionDenied


def require_client(request):
    if role(request) != CLIENT:
        raise PermissionDenied


def client_side_users(project):
    """Ids of the client and consultant people on the job: their defects show on the client page."""
    return ProjectMember.objects.filter(project=project, role__in=CLIENT_ROLES).values('user_id')


def client_defects(project):
    """Defects reported by the client side (anyone on the job as client or consultant)."""
    from projects.models import Issue
    return Issue.objects.filter(project=project, issue_type='DEFECT', created_by__in=client_side_users(project))


def tab_for(request, project):
    """The project tab for this person, or None."""
    from django.urls import reverse
    who = role(request)
    if project.section != 'delivery' or who is None:
        return None
    org, pk = request.org.slug, project.pk
    if who == STAFF:
        return ('closeout', 'Close-out', reverse('closeout_overview', args=[org, pk]), None)
    return ('closeout', 'Defects and handover', reverse('closeout_client', args=[org, pk]), None)
