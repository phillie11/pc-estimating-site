"""
The public QR sign-in page: /sign-in/<token>/. No login needed; the poster's secret token is
the only credential, and all it allows is signing in to and out of that one job.

What it may show: the job's name, and to someone signed in, their own name and the time they
signed in. Nothing else about the job or the company (not the address, client, company name,
who else is on site, or whether a token ever existed): the template is given plain strings only,
never the project or organisation object.

  * Unknown, replaced, archived, not-yet-won and completed jobs' tokens all get the same 404 page.
  * Rate limits: per address in security/ratelimit.py RULES ('site-signin', 'site-signin-view'),
    and per poster here (GATE_LIMIT, sign-ins only), so a script can't flood one job's roll call.
  * Someone with an account in the company signs in with one tap (recorded against their login).
    Anyone else types a name and company. A visitor can only sign themselves out, from the phone
    they signed in on (a signed cookie, scoped to this poster's path); the site team can sign anyone
    out. Nobody can sign out someone else from here, because a false sign-out would drop a person
    off the fire roll call.
  * No caching, no indexing, and the CSRF token on every form.
"""
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from projects.models import Project, ProjectMember
from security import ratelimit
from security.middleware import too_many_requests
from tenancy.context import org_context
from tenancy.models import Membership

from . import rules
from .forms import VisitorForm
from .models import SiteAttendance, SiteGate

GATE_LIMIT = (400, 3600)  # sign-ins per poster per hour, from every phone together (sign-outs aren't capped)
MAX_TOKEN = 64


def _gate(token):
    """The poster behind a token, or None if it doesn't exist or its job isn't open for sign-in."""
    if not token or len(token) > MAX_TOKEN:
        return None
    # No workspace is open on this page: the secret token pins the row, so look it up unscoped, as invite links do
    gate = SiteGate.all_objects.select_related('project', 'organization').filter(token=token).first()
    if gate is None:
        return None
    project = gate.project
    if project.is_archived or project.stage not in Project.DELIVERY_STAGES or not gate.organization.has_access:
        return None
    return gate


def _invalid(request):
    resp = render(request, 'safety/public_invalid.html', status=404)
    return _private(resp)


def _private(resp):
    resp['Cache-Control'] = 'no-store, max-age=0'
    resp['X-Robots-Tag'] = 'noindex, nofollow'
    return resp


def _cookie_name(gate):
    return f'sl_site_{gate.pk}'


def _salt(gate):
    return f'safety.site-visitor.{gate.token}'  # replacing the poster's token ends every visitor cookie too


def _membership(request, gate):
    if not request.user.is_authenticated:
        return None
    return Membership.objects.filter(organization=gate.organization, user=request.user, is_active=True).first()


def _open_today(gate, **filters):
    since = rules.on_site_since(timezone.localtime())
    return SiteAttendance.objects.filter(project=gate.project, signed_in_at__gte=since, signed_out_at__isnull=True,
                                         **filters).order_by('-signed_in_at').first()


def _visitor_attendance(request, gate):
    try:
        pk = request.get_signed_cookie(_cookie_name(gate), salt=_salt(gate),
                                       max_age=rules.VISITOR_COOKIE_HOURS * 3600)
    except (KeyError, signing.BadSignature, ValueError):
        return None
    if not str(pk).isdigit():
        return None
    return _open_today(gate, pk=int(pk), user__isnull=True)


def _current(request, gate, membership):
    if membership is not None:
        return _open_today(gate, user=request.user)
    return _visitor_attendance(request, gate)


def _company_for(gate, user, membership):
    if membership.is_internal:
        return gate.organization.name, SiteAttendance.KIND_STAFF
    pm = ProjectMember.objects.filter(project=gate.project, user=user).select_related('subcontractor').first()
    company = (pm.company_name or (pm.subcontractor.company_name if pm.subcontractor_id else '')) if pm else ''
    return company, SiteAttendance.KIND_COLLABORATOR


def _over_gate_limit(gate):
    limit, window = GATE_LIMIT
    return not ratelimit.hit('site-signin-gate', gate.pk, limit, window)


@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def site_signin(request, token):
    gate = _gate(token)
    if gate is None:
        return _invalid(request)
    with org_context(gate.organization):
        membership = _membership(request, gate)
        current = _current(request, gate, membership)
        form = VisitorForm(request.POST or None, initial={'name': request.user.full_name}
                           if request.user.is_authenticated and membership is None else None)
        if request.method == 'POST':
            if current is not None:
                return redirect(gate.public_path())  # already signed in: don't make a second entry
            # The poster's cap counts sign-ins only: a post that signs nobody in mustn't use it up
            creating = membership is not None or form.is_valid()
            if creating and _over_gate_limit(gate):
                return too_many_requests(request, ratelimit.retry_after(GATE_LIMIT[1]))
            if membership is not None:
                company, kind = _company_for(gate, request.user, membership)
                a = SiteAttendance.objects.create(project=gate.project, user=request.user,
                                                  name=request.user.full_name or request.user.email, company=company,
                                                  kind=kind)
                messages.success(request, f'Signed in at {timezone.localtime(a.signed_in_at):%H:%M}. '
                                          'Remember to sign out when you leave.')
                return redirect(gate.public_path())
            if form.is_valid():
                a = SiteAttendance.objects.create(project=gate.project, name=form.cleaned_data['name'],
                                                  company=form.cleaned_data['company'],
                                                  kind=SiteAttendance.KIND_VISITOR)
                messages.success(request, f'Signed in at {timezone.localtime(a.signed_in_at):%H:%M}. '
                                          'Scan the poster again to sign out when you leave.')
                resp = redirect(gate.public_path())
                resp.set_signed_cookie(_cookie_name(gate), str(a.pk), salt=_salt(gate),
                                       max_age=rules.VISITOR_COOKIE_HOURS * 3600, path=gate.public_path(),
                                       secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite='Lax')
                return resp
        ctx = {
            'site_name': gate.project.name,
            'signed_in_at': timezone.localtime(current.signed_in_at) if current else None,
            'who': current.name if current else '',
            'member_name': (request.user.full_name or request.user.email) if membership is not None else '',
            'form': form,
            'signout_url': gate.public_path() + 'out/',
        }
        return _private(render(request, 'safety/public_signin.html', ctx))


@never_cache
@require_POST
def site_signout(request, token):
    gate = _gate(token)
    if gate is None:
        return _invalid(request)
    with org_context(gate.organization):
        # No poster cap here: signing out only closes your own row, and a full poster mustn't leave
        # people who have gone home on the fire roll call (the per-address limit still applies)
        current = _current(request, gate, _membership(request, gate))
        if current is None:
            messages.info(request, 'You are not signed in to this site on this phone.')
        else:
            current.signed_out_at = timezone.now()
            current.save(update_fields=['signed_out_at'])
            messages.success(request, f'Signed out at {timezone.localtime(current.signed_out_at):%H:%M}. '
                                      'Thank you, travel safely.')
        resp = redirect(gate.public_path())
        resp.delete_cookie(_cookie_name(gate), path=gate.public_path(), samesite='Lax')
        return resp
