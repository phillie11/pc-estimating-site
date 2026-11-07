"""
The read-only API (/api/v1/...) and Settings → API.

Every API call goes through `_call()`:

  1. Only GET and HEAD (405 otherwise). The API never changes anything.
  2. The token from `Authorization: Bearer ...` (api/auth.py): 401 if it doesn't work, 402 if the
     workspace is locked, 429 over the limit.
  3. The token's resource group must cover the endpoint (403 otherwise).
  4. The rest runs inside org_context(the token's company), so every TenantModel query is pinned to it
     and another company's id is simply not found (404). Nothing reads the session: request.user is
     anonymous for the whole call, so a browser that happens to be signed in adds nothing.
  5. Every call, refused or not, is written to the token's log (api.ApiAccess) once the token is known.

Settings → API (owners and admins) makes, lists and revokes tokens and has the human-readable docs.
"""
import math

from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from tenancy.context import org_context
from tenancy.models import log_activity
from tenancy.permissions import admin_required

from . import auth, openapi
from .forms import TokenForm
from .models import SCOPE_DOCUMENTS, ApiAccess, ApiToken
from .resources import BadParameter, Context, by_name, resources

PAGE_SIZE, MAX_PAGE_SIZE = 50, 200
PAGING_PARAMS = ('page', 'page_size')
SAFE = ('GET', 'HEAD')


def error(status, message, **extra):
    resp = JsonResponse({'error': {'status': status, 'message': message, **extra}}, status=status)
    resp['Cache-Control'] = 'no-store'
    if status == 401:
        resp['WWW-Authenticate'] = 'Bearer realm="siteline"'
    if status == 429:
        resp['Retry-After'] = str(auth.WINDOW)
    return resp


def ok(data):
    resp = JsonResponse(data, json_dumps_params={'ensure_ascii': False})
    resp['Cache-Control'] = 'no-store'
    return resp


def _call(request, scope, handler):
    if request.method not in SAFE:
        resp = error(405, 'The API is read only: use GET.')
        resp['Allow'] = 'GET, HEAD'
        return resp
    request.user = AnonymousUser()  # the token is the only credential; never the session
    token, problem = auth.authenticate(request)
    if problem is not None:
        status, message = problem
        if token is not None:
            with org_context(token.organization):
                auth.record(token, request, status)
        return error(status, message)
    with org_context(token.organization):
        request.org = token.organization
        try:
            if scope and not token.allows(scope):
                raise PermissionDenied(f'This token can\'t read {scope}. An owner or admin can make one that does '
                                       'in Settings → API.')
            resp = handler(Context(token, request))
        except Http404:
            resp = error(404, 'Not found. It may not exist, or it may belong to a job this token can\'t see '
                              '(archived jobs are left out).')
        except BadParameter as exc:
            resp = error(400, str(exc))
        except PermissionDenied as exc:
            resp = error(403, str(exc) or 'This token can\'t read that.')
        except Exception:
            # A call that fails is still a call: log it, without letting the log hide the real error
            try:
                auth.record(token, request, 500)
            except Exception:  # noqa: BLE001 - e.g. the database is what failed
                pass
            raise
        auth.record(token, request, resp.status_code)
    return resp


def _page_numbers(request):
    def number(name, default, low, high):
        raw = request.GET.get(name, '').strip()
        if not raw:
            return default
        if not raw.isdigit() or not raw.isascii():
            raise BadParameter(f'{name} must be a whole number.')
        value = int(raw)
        if not low <= value <= high:
            raise BadParameter(f'{name} must be between {low} and {high}.')
        return value
    return number('page', 1, 1, 10 ** 6), number('page_size', PAGE_SIZE, 1, MAX_PAGE_SIZE)


def _apply_filters(resource, qs, ctx):
    known = {f.name for f in resource.filters} | set(PAGING_PARAMS)
    unknown = sorted(set(ctx.request.GET) - known)
    if unknown:
        raise BadParameter(f'Unknown parameter {", ".join(unknown)}. This endpoint takes: '
                           f'{", ".join(sorted(known))}.')
    for f in resource.filters:
        value = ctx.request.GET.get(f.name, '').strip()
        if value:
            qs = f.apply(qs, value, ctx)
    return qs


def _page_url(ctx, page):
    q = ctx.request.GET.copy()
    q['page'] = page
    return ctx.absolute(ctx.request.path + '?' + q.urlencode())


def _list(resource, ctx):
    page, size = _page_numbers(ctx.request)
    qs = _apply_filters(resource, resource.queryset(ctx), ctx)
    count = qs.count()
    pages = max(1, math.ceil(count / size))
    if page > pages and count:
        raise Http404
    rows = list(qs[(page - 1) * size:page * size])
    if resource.prepare:
        resource.prepare(rows, ctx, False)
    return ok({
        'count': count, 'page': page, 'page_size': size, 'pages': pages,
        'next': _page_url(ctx, page + 1) if page < pages else None,
        'previous': _page_url(ctx, page - 1) if page > 1 else None,
        'results': [resource.serialize(r, ctx) for r in rows],
    })


def _detail(resource, ctx, pk):
    allowed = {f.name for f in resource.filters if f.name == 'month'}
    unknown = sorted(set(ctx.request.GET) - allowed)
    if unknown:
        raise BadParameter(f'Unknown parameter {", ".join(unknown)}.')
    row = resource.queryset(ctx).filter(pk=pk).first()
    if row is None:
        raise Http404
    if resource.prepare:
        resource.prepare([row], ctx, True)
    return ok(resource.serialize(row, ctx, detail=True))


@csrf_exempt  # read only: anything but GET or HEAD is answered 405 before it is looked at
def resource_list(request, name):
    resource = by_name(name)
    return _call(request, resource.scope, lambda ctx: _list(resource, ctx))


@csrf_exempt
def resource_detail(request, name, pk):
    resource = by_name(name)
    return _call(request, resource.scope, lambda ctx: _detail(resource, ctx, pk))


@csrf_exempt
def index(request):
    def handler(ctx):
        token = ctx.token
        return ok({
            'company': ctx.org.name, 'token': token.name, 'scopes': sorted(token.scope_set),
            'documentation': ctx.absolute(reverse('api_openapi')),
            'endpoints': [{'name': r.name, 'title': r.title, 'url': ctx.absolute(reverse(r.list_url_name())),
                           'allowed': token.allows(r.scope)} for r in resources()],
        })
    return _call(request, None, handler)


@csrf_exempt
def document_download(request, doc_id, rev_id):
    def handler(ctx):
        from projects import files
        from projects.models import Document, DocumentRevision, FileScan
        from .resources import on_visible
        doc = get_object_or_404(on_visible(Document.objects.all()).select_related('project'), pk=doc_id)
        rev = get_object_or_404(DocumentRevision.objects.filter(document=doc), pk=rev_id)
        if files.scanning_enabled():
            # Answer in JSON rather than the scan gate's HTML pages
            status = files.scan_status(rev.file, ctx.org)
            if status is None:
                return error(409, 'The file is still being virus-checked. Try again in a minute.')
            if status != FileScan.CLEAN:
                files.alert_blocked(ctx.org, rev.file.name, status, rev.original_name, doc.project)
                return error(403, 'This file failed its virus check, so it can\'t be downloaded.')
        return files.serve(request, rev.file, rev.original_name, project=doc.project,
                           label=f'API token "{ctx.token.name}": {doc.reference or doc.title} rev {rev.revision}')
    return _call(request, SCOPE_DOCUMENTS, handler)


@never_cache
def openapi_json(request):
    """The OpenAPI 3 description. Public: it lists the endpoints and fields, never any company's data."""
    if request.method not in SAFE:
        resp = error(405, 'Use GET.')
        resp['Allow'] = 'GET, HEAD'
        return resp
    return JsonResponse(openapi.document(request.build_absolute_uri('/api/v1/').rstrip('/')),
                        json_dumps_params={'indent': 2})


# ---------------------------------------------------------------------------
# Settings → API (owners and admins)
# ---------------------------------------------------------------------------

def _settings_ctx(request, **extra):
    tokens = list(ApiToken.objects.select_related('created_by', 'revoked_by'))
    for t in tokens:
        t.maker_ok = auth.maker_still_admin(t)
    live = [t for t in tokens if t.is_live]
    return {'tokens': tokens, 'live': live, 'old': [t for t in tokens if not t.is_live],
            'api_base': request.build_absolute_uri('/api/v1/'), 'limit': auth.LIMIT,
            'window_minutes': auth.WINDOW // 60, **extra}


@admin_required
def api_settings(request, org_slug):
    form = TokenForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        token, text = auth.create_token(request.org, request.user, form.cleaned_data['name'],
                                        form.cleaned_data['scopes'], form.expires_at())
        log_activity(request, 'made API token', token.name)
        resp = render(request, 'api/created.html', {'token': token, 'text': text,
                                                    'api_base': request.build_absolute_uri('/api/v1/')})
        resp['Cache-Control'] = 'no-store'  # the only time the token is shown: never keep a copy of the page
        return resp
    return render(request, 'api/settings.html', _settings_ctx(request, form=form))


@admin_required
def api_token_detail(request, org_slug, token_id):
    token = get_object_or_404(ApiToken.objects.select_related('created_by', 'revoked_by'), pk=token_id)
    token.maker_ok = auth.maker_still_admin(token)
    calls = ApiAccess.objects.filter(token=token).order_by('-at', '-pk')[:50]
    return render(request, 'api/token.html', {'token': token, 'calls': calls, 'keep_days': auth.KEEP_DAYS})


@admin_required
def api_token_revoke(request, org_slug, token_id):
    token = get_object_or_404(ApiToken, pk=token_id)
    if request.method != 'POST':
        return redirect('api_token_detail', org_slug=org_slug, token_id=token.pk)
    if token.revoked_at is None:
        ApiToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now(), revoked_by=request.user)
        log_activity(request, 'revoked API token', token.name)
        messages.success(request, f'"{token.name}" revoked. Anything using it gets "token does not work" from now on.')
    return redirect('api_settings', org_slug=org_slug)


@admin_required
def api_docs(request, org_slug):
    return render(request, 'api/docs.html', {'resources': resources(), 'api_base': request.build_absolute_uri('/api/v1/'),
                                             'limit': auth.LIMIT, 'window_minutes': auth.WINDOW // 60,
                                             'page_size': PAGE_SIZE, 'max_page_size': MAX_PAGE_SIZE,
                                             'openapi_url': request.build_absolute_uri(reverse('api_openapi'))})
