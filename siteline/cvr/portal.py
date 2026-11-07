"""
Signing a daywork sheet in the client's portal, as an alternative to signing on the site team's phone
(pending owner decision; built as the recommended default).

  staff        on the sheet: "Ask them to sign in their portal" picks one client or consultant on the
               job (the client, their representative or the contract administrator). It is on that
               person's Needs me until they sign it, the site team signs it on the phone instead, the
               request is withdrawn or the sheet is cancelled or sent on.
  that person  "Daywork sheets" under Waiting on you: the sheet's work, the hours, plant and materials
               (never rates or money), its photos, and the same signature pad and checks as the phone
               (cvr.forms.SignatureForm through the `sign` transition: a line on it, the signature picture
               and a name). A sheet changed since they opened it is refused, so they sign what they saw.
  anyone else  a client or consultant who wasn't asked sees nothing of it (404); subcontractors and
               anyone else on the job get a 403; staff are sent to the staff page; another company 404.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.exceptions import InvalidTransition, RecordConflict
from core.workflow import apply_transition
from projects.access import external_role, get_project, is_internal
from projects.files import serve
from projects.models import Project, ProjectMember
from projects.views import project_context
from tenancy import notify
from tenancy.models import log_activity

from .forms import SignatureForm
from .models import DayworkPhoto, DayworkSheet

CLIENT_ROLES = (ProjectMember.ROLE_CLIENT, ProjectMember.ROLE_CONSULTANT)
SIGN_STAGES = Project.DELIVERY_STAGES + (Project.STAGE_COMPLETE,)


def client_side(project):
    """The clients and consultants on the job who can be asked to sign, as users."""
    from django.contrib.auth import get_user_model
    ids = ProjectMember.objects.filter(project=project, role__in=CLIENT_ROLES).values('user_id')
    return get_user_model().objects.filter(pk__in=ids, is_active=True).order_by('full_name')


def is_requested_signer(sheet, user):
    """The client or consultant the site team asked to sign this sheet, still on the job, while it is unsigned."""
    if user is None or sheet.status != DayworkSheet.DRAFT or sheet.sign_requested_from_id != getattr(user, 'pk', None):
        return False
    return ProjectMember.objects.filter(project_id=sheet.project_id, user=user, role__in=CLIENT_ROLES).exists()


# --- staff: ask, and stop asking ------------------------------------------------------

def ask(sheet, actor, signer):
    """Put the sheet on `signer`'s Needs me to sign in their portal, and tell them. Raises InvalidTransition."""
    if sheet.status != DayworkSheet.DRAFT:
        raise InvalidTransition(f'{sheet.reference} is {sheet.get_status_display().lower()}, so it can\'t be signed.')
    if not sheet.lines.exists():
        raise InvalidTransition('Add at least one line of labour, plant or materials before asking for a signature.')
    if signer is None or not client_side(sheet.project).filter(pk=signer.pk).exists():
        raise InvalidTransition('Choose someone on the client side of this job to sign it.')
    sheet.sign_requested_from, sheet.sign_requested_by, sheet.sign_requested_at = signer, actor, timezone.now()
    sheet.save(actor=actor, audit_action='signature requested', audit_note=f'Asked {signer.full_name} to sign in the portal')
    notify.alert([signer], f'Daywork sheet to sign: {sheet.reference} {sheet.title}',
                 reverse('daywork_portal_sign', args=[sheet.organization.slug, sheet.project_id, sheet.pk]),
                 project=sheet.project, actor=actor, org=sheet.organization)
    return sheet


def stop_asking(sheet, actor):
    if sheet.sign_requested_from_id is None:
        return sheet
    who = sheet.sign_requested_from.full_name if sheet.sign_requested_from else 'them'
    sheet.sign_requested_from = sheet.sign_requested_by = sheet.sign_requested_at = None
    sheet.save(actor=actor, audit_action='signature request withdrawn', audit_note=f'No longer waiting for {who}')
    return sheet


# --- the client's pages ---------------------------------------------------------------

def _client_project(request, pk, staff_page='daywork_list'):
    project = get_project(request, pk)
    if is_internal(request):
        return project, redirect(staff_page, org_slug=request.org.slug, pk=pk)
    if external_role(request) not in CLIENT_ROLES or project.stage not in SIGN_STAGES:
        raise PermissionDenied
    return project, None


def mine(request, project):
    """Sheets on this job the site team asked this person to sign (signed, waiting or since sent on)."""
    return DayworkSheet.objects.filter(project=project, sign_requested_from=request.user)


def tab_for(request, project):
    """'Daywork sheets' under Waiting on you, once this client or consultant has been asked to sign one."""
    if is_internal(request) or external_role(request) not in CLIENT_ROLES or project.stage not in SIGN_STAGES:
        return None
    n = DayworkSheet.objects.filter(project=project).filter(
        Q(sign_requested_from=request.user) | Q(signature_recorded_by=request.user)).aggregate(
        all=Count('pk'), waiting=Count('pk', filter=Q(status=DayworkSheet.DRAFT, sign_requested_from=request.user)))
    if not n['all']:
        return None
    return ('dayworks', 'Daywork sheets', reverse('daywork_portal', args=[request.org.slug, project.pk]),
            n['waiting'] or None)


def daywork_list(request, org_slug, pk):
    project, staff = _client_project(request, pk)
    if staff:
        return staff
    rows = list(DayworkSheet.objects.filter(project=project).filter(
        Q(sign_requested_from=request.user) | Q(signature_recorded_by=request.user)).order_by('-work_date', '-number'))
    waiting = [s for s in rows if s.status == DayworkSheet.DRAFT and s.sign_requested_from_id == request.user.pk]
    done = [s for s in rows if s not in waiting and s.status != DayworkSheet.DRAFT]
    return render(request, 'cvr/portal_daywork_list.html', project_context(
        request, project, 'dayworks', waiting=waiting, done=done))


class PortalSignatureForm(SignatureForm):
    """The phone's form, worded for the person signing it themselves."""

    def __init__(self, *args, user=None, role='CLIENT', **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].label = 'Your name'
        self.fields['role'].label = 'You are signing as'
        self.fields['signature'].help_text = ('Sign in the box with a finger or mouse. No touch screen? Upload a '
                                              'photo of your signature on paper instead.')
        self.fields['confirm'].label = 'I have checked the hours, plant and materials on this sheet'
        self.fields['confirm'].error_messages['required'] = 'Tick to confirm you have checked the sheet.'
        if not self.is_bound:
            self.initial.setdefault('name', getattr(user, 'full_name', ''))
            self.initial.setdefault('role', role)


def daywork_sign(request, org_slug, pk, sheet_id):
    project, staff = _client_project(request, pk)
    if staff:
        return redirect('daywork_sign', org_slug=org_slug, pk=pk, sheet_id=sheet_id)
    sheet = get_object_or_404(mine(request, project).select_related('sign_requested_by'), pk=sheet_id)
    if not sheet.waiting_for_portal_signature:
        messages.info(request, f'{sheet.reference} is {sheet.get_status_display().lower()}. There is nothing to sign.')
        return redirect('daywork_portal', org_slug=org_slug, pk=pk)
    role = 'CA' if external_role(request) == ProjectMember.ROLE_CONSULTANT else 'CLIENT'
    form = PortalSignatureForm(request.POST or None, request.FILES or None, user=request.user, role=role)
    if request.method == 'POST' and form.is_valid():
        posted = request.POST.get('version', '')
        if not (posted.isdigit() and int(posted) == sheet.version):
            messages.error(request, f'The site team changed {sheet.reference} after you opened it. Check it again, '
                                    'then sign.')
            return redirect('daywork_portal_sign', org_slug=org_slug, pk=pk, sheet_id=sheet.pk)
        d = form.cleaned_data
        try:
            apply_transition(sheet, 'sign', request.user, note=f'Signed by {d["name"]} in the portal',
                             signature=d['signature'], signer=d['name'].strip(), role=d['role'])
        except InvalidTransition as e:
            form.add_error(None, str(e))
        except RecordConflict:
            messages.error(request, f'{sheet.reference} was changed while you were signing. Check it again, then sign.')
            return redirect('daywork_portal_sign', org_slug=org_slug, pk=pk, sheet_id=sheet.pk)
        else:
            log_activity(request, 'signed in the portal', sheet.reference, project=project,
                         url=sheet.get_absolute_url(), internal_only=True)
            told = [u for u in (sheet.sign_requested_by, project.manager) if u is not None]
            notify.alert(told, f'{sheet.reference} signed by {sheet.signed_by_name} in their portal',
                         sheet.get_absolute_url(), project=project, actor=request.user, org=request.org)
            messages.success(request, f'Thank you. {sheet.reference} is signed and the site team has been told. The '
                                      'value is agreed separately.')
            return redirect('daywork_portal', org_slug=org_slug, pk=pk)
    return render(request, 'cvr/portal_daywork_sign.html', project_context(
        request, project, 'dayworks', sheet=sheet, form=form, lines=list(sheet.lines.all()),
        photos=list(sheet.photos.all())))


def daywork_photo(request, org_slug, pk, sheet_id, photo_id, size):
    project, staff = _client_project(request, pk)
    if staff:
        return redirect('daywork_photo', org_slug=org_slug, pk=pk, sheet_id=sheet_id, photo_id=photo_id, size=size)
    if size not in ('thumb', 'full'):
        raise Http404
    sheet = get_object_or_404(mine(request, project), pk=sheet_id)
    photo = get_object_or_404(DayworkPhoto, sheet=sheet, pk=photo_id)
    field = photo.thumbnail if size == 'thumb' and photo.thumbnail else photo.image
    return serve(request, field, project=project, label=f'{sheet.reference} photo')


# --- Needs me for the client side ---------------------------------------------------------

def client_items(request):
    """Daywork sheets the site team asked this person to sign in their portal, still unsigned."""
    if request.membership is None or is_internal(request):
        return []
    from actions.registry import PRIORITY_NORMAL, Item
    on_job = ProjectMember.objects.filter(project=OuterRef('project_id'), user=request.user, role__in=CLIENT_ROLES)
    rows = DayworkSheet.objects.filter(sign_requested_from=request.user, status=DayworkSheet.DRAFT,
                                       project__archived_at__isnull=True).filter(Exists(on_job)) \
        .select_related('project', 'project__organization')
    slug = request.org.slug
    return [Item(kind='daywork_sign', title=s.title, ref=s.reference, project=s.project, due=s.due_date,
                 url=reverse('daywork_portal_sign', args=[slug, s.project_id, s.pk]),
                 action='Check the hours, plant and materials, then sign', status_label='To sign', tone='accent',
                 priority=PRIORITY_NORMAL, kind_label='Daywork sheets', key=f'dw-sign:{s.pk}') for s in rows]
