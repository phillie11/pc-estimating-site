"""
The drawing viewer: a revision drawn in the browser by pdf.js, with snag and RFI pins.

Every view fetches the project with projects.access.get_project() and the document and revision
through documents.access, so the viewer reaches nothing the person couldn't already download.
The PDF itself is sent by projects.files.serve(), with the same malware-scan gate and download
log as the download button.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import prefetch_related_objects
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from documents.access import annotate, display_revision, visible_documents, visible_ids
from documents.views import docs_context
from projects.access import get_project, is_internal
from projects.files import serve
from projects.models import revision_rank
from quality import pins as quality_pins
from tenancy.models import log_activity

from . import markup
from . import pins as pinlib
from .access import (best_revision, can_mark_up, can_raise_snag, can_remove, can_view, effective_share,
                     get_doc_and_revision, is_pdf, share_choices, viewable_revisions, visible_issues, visible_layers, visible_pins,
                     visible_revision_ids, visible_rfis)
from .models import InvalidPin, MarkupLayer, PinLocked


def _withdrawn_for_external(request, project, doc, rev):
    """Externals may see that a revision was withdrawn, but not take a copy or view it (as documents.views)."""
    if rev.is_withdrawn and not is_internal(request):
        return render(request, 'documents/withdrawn.html', docs_context(request, project, 'register', doc=doc,
                                                                         rev=rev), status=403)
    return None


def drawing_viewer(request, org_slug, pk, doc_id, rev_id):
    project = get_project(request, pk)
    doc, rev = get_doc_and_revision(request, project, doc_id, rev_id)
    refused = _withdrawn_for_external(request, project, doc, rev)
    if refused is not None:
        return refused
    if not is_pdf(rev):
        messages.info(request, f'{rev.original_name} is not a PDF, so it can only be downloaded.')
        return redirect('revision_detail', org_slug=org_slug, pk=pk, doc_id=doc.pk, rev_id=rev.pk)
    if not can_view(request, rev):
        raise Http404
    seen = visible_ids(request, project)
    annotate(request, [rev], seen)
    # Every revision of the drawing in one query: the current one, and those this person may open (mark-ups, compare)
    prefetch_related_objects([doc], 'revisions')
    current = doc.current
    current = current if current is not None and current.pk != rev.pk else None
    revs = [r for r in doc.revisions.all() if (seen is None or r.pk in seen) and can_view(request, r)]
    if current is not None and seen is not None and current.pk not in seen:
        current = None  # not issued to them: don't name it
    current_viewable = current if current is not None and can_view(request, current) else None

    internal = is_internal(request)
    can_pin = not project.is_archived and not rev.is_withdrawn
    pins = [pinlib.describe(request, project, p, rev=rev, seen=seen) for p in visible_pins(request, project, doc)]
    pins += quality_pins.viewer_pins(request, project, doc, rev)
    choices = []
    if can_pin:
        issues = visible_issues(request, project).order_by('-number')[:200]
        rfis = visible_rfis(request, project).order_by('-number')[:200]
        choices = pinlib.record_choices(request, project, issues, rfis)
    config = {
        'pdf_url': reverse('drawing_pdf', args=[org_slug, project.pk, doc.pk, rev.pk]),
        'pdfjs': static('vendor/pdfjs/pdf.min.js'),
        'worker': static('vendor/pdfjs/pdf.worker.min.js'),
        'fonts': static('vendor/pdfjs/standard_fonts/'),
        'doc': doc.pk,
        'rev': rev.pk,
        'pages': rev.page_count or None,
        'pins': pins,
        'focus': request.GET.get('pin', ''),
        'can_pin': can_pin,
        'pin_url': reverse('pin_create', args=[org_slug, project.pk, doc.pk]),
        'new_snag_url': reverse('issue_create', args=[org_slug, project.pk]) if can_pin and can_raise_snag(
            request) else '',
        'new_rfi_url': reverse('rfi_create', args=[org_slug, project.pk]) if can_pin else '',
        # Fire-stopping penetrations (quality/pins.py): shown with the pins, and "New fire-stopping" for staff
        'new_firestop_url': quality_pins.new_url(request, project) if can_pin else '',
        'choices': choices,
    }
    # Mark-up layers: this person's own and those shared with them, on any revision they may open
    layers = list(visible_layers(request, project, doc, revs))
    layers.sort(key=lambda la: (la.revision_id != rev.pk, la.owner_id != request.user.pk, -la.updated_at.timestamp()))
    mine = next((la for la in layers if la.owner_id == request.user.pk and la.revision_id == rev.pk), None)
    can_draw = can_mark_up(request, project, rev)
    config.update({
        'markup_js': static('viewer/markup.js'),
        'layers': [layer_info(request, project, la, rev) for la in layers],
        'mine': {'id': mine.pk, 'version': mine.version, 'shapes': mine.shapes} if mine else None,
        'can_draw': can_draw,
        'save_url': reverse('markup_save', args=[org_slug, project.pk, doc.pk, rev.pk]) if can_draw else '',
        'scale': doc.scale,
        'name': f'{doc.reference or doc.title} rev {rev.revision}',
    })
    return render(request, 'viewer/viewer.html', docs_context(
        request, project, 'register', doc=doc, rev=rev, current=current, current_viewable=current_viewable,
        config=config, pins=pins, can_pin=can_pin, internal=internal, layers=config['layers'], mine=mine,
        mine_info=next((la for la in config['layers'] if la['mine'] and la['here']), None),
        can_draw=can_draw, share_choices=share_choices(request), compare=len(revs) > 1,
        download_url=reverse('document_download', args=[org_slug, project.pk, doc.pk, rev.pk])))


def drawing_pdf(request, org_slug, pk, doc_id, rev_id):
    """The PDF bytes for the viewer: the same checks as a download, but always through this site."""
    project = get_project(request, pk)
    doc, rev = get_doc_and_revision(request, project, doc_id, rev_id)
    refused = _withdrawn_for_external(request, project, doc, rev)
    if refused is not None:
        return refused
    if not can_view(request, rev):
        raise Http404
    return serve(request, rev.file, rev.original_name, project=project,
                 label=f'{doc.reference or doc.title} rev {rev.revision} (viewer)', through_app=True)


def _error(message, status=400):
    return JsonResponse({'error': message}, status=status)


@require_POST
def pin_create(request, org_slug, pk, doc_id):
    """Pin an existing snag or RFI the person can see. New ones are pinned by their create forms (viewer.pins)."""
    project = get_project(request, pk)
    rev_id = request.POST.get('rev', '')
    if not rev_id.isdigit() or not rev_id.isascii():
        raise Http404
    try:
        doc, rev, page, x, y = pinlib.placement(request, project, doc_id, int(rev_id), request.POST.get('page'),
                                                request.POST.get('x'), request.POST.get('y'))
    except InvalidPin as e:
        return _error(str(e))
    kind, _, record_id = request.POST.get('record', '').partition(':')
    if not record_id.isdigit() or not record_id.isascii():
        return _error('Choose a snag or RFI to pin.')
    if kind == 'issue':
        record = get_object_or_404(visible_issues(request, project), pk=int(record_id))
        target = {'issue': record}
    elif kind == 'rfi':
        record = get_object_or_404(visible_rfis(request, project), pk=int(record_id))
        target = {'rfi': record}
    else:
        return _error('Choose a snag or RFI to pin.')
    try:
        pin = pinlib.create_pin(request, project, doc, rev, page, x, y, **target)
    except PinLocked as e:
        return _error(str(e), status=403)
    except IntegrityError:
        return _error('Someone else pinned this at the same moment. Reload the page and try again.', status=409)
    log_activity(request, 'pinned', f'{record.reference} to {doc.label}', project=project,
                 url=pinlib.viewer_url(request, project, doc, rev, pin), internal_only=True)
    return JsonResponse(pinlib.describe(request, project, pin, rev=rev, seen=visible_revision_ids(request, project)),
                        status=201)


@require_POST
def pin_delete(request, org_slug, pk, doc_id, pin_id):
    project = get_project(request, pk)
    doc = get_object_or_404(visible_documents(request, project), pk=doc_id)
    pin = get_object_or_404(visible_pins(request, project, doc), pk=pin_id)
    if not can_remove(request, pin):
        raise PermissionDenied
    pin.delete()
    return JsonResponse({'ok': True})


def pin_open(request, org_slug, pk, pin_id):
    """From a snag or RFI page: open the drawing at its pin, on the revision this person should be looking at."""
    project = get_project(request, pk)
    pin = get_object_or_404(visible_pins(request, project), pk=pin_id)
    doc = get_object_or_404(visible_documents(request, project), pk=pin.document_id)
    rev = best_revision(request, project, doc)
    return redirect(pinlib.viewer_url(request, project, doc, rev, pin))


# ---------------------------------------------------------------------------
# Mark-up layers (viewer/markup.py checks the shapes; viewer/access.py decides who sees which)
# ---------------------------------------------------------------------------

STALE = 'This mark-up was changed in another window or on another device. Reload the page to see the latest.'


def layer_info(request, project, layer, rev=None):
    """What the viewer needs to list a layer. Words only: the browser shows them as text, never as HTML."""
    mine = layer.owner_id == request.user.pk
    who = 'You' if mine else (layer.owner.full_name or layer.owner.email)
    share = effective_share(request, layer)
    return {
        'id': layer.pk,
        'mine': mine,
        'owner': who,
        'rev': layer.revision_id,
        'revision': layer.revision.revision,
        'here': rev is not None and layer.revision_id == rev.pk,
        'share': share,
        'share_label': dict(MarkupLayer.SHARE_CHOICES)[share],
        'summary': markup.summary(layer.shapes),
        'count': layer.shape_count,
        'version': layer.version,
        'data_url': reverse('markup_data', args=[request.org.slug, project.pk, layer.document_id, layer.pk]),
    }


def _layer_json(request, project, layer, rev):
    return JsonResponse({**layer_info(request, project, layer, rev), 'shapes': layer.shapes})


def _markup_target(request, project, doc_id, rev_id):
    """The revision someone is drawing on: one they may view (else 404) and may mark up (else 403)."""
    doc, rev = get_doc_and_revision(request, project, doc_id, rev_id)
    if not can_view(request, rev):
        raise Http404
    if not can_mark_up(request, project, rev):
        raise PermissionDenied('This revision has been withdrawn, so it can\'t be marked up.')
    return doc, rev


@require_POST
def markup_save(request, org_slug, pk, doc_id, rev_id):
    """Save the whole of this person's own layer on a revision (made on the first save)."""
    project = get_project(request, pk)
    doc, rev = _markup_target(request, project, doc_id, rev_id)
    try:
        data = markup.parse(request.POST.get('data', ''), rev.page_count)
    except markup.InvalidMarkup as e:
        return _error(str(e))
    version = request.POST.get('version', '').strip()
    with transaction.atomic():
        layer = MarkupLayer.objects.select_for_update().filter(revision=rev, owner=request.user).first()
        if layer is None:
            if version not in ('', '0'):
                return _error(STALE, status=409)  # deleted in another window
            layer = MarkupLayer(project=project, document=doc, revision=rev, owner=request.user, data=data,
                                shape_count=len(data['shapes']))
            try:
                with transaction.atomic():
                    layer.save()
            except IntegrityError:
                return _error(STALE, status=409)
        else:
            if version != str(layer.version):
                return _error(STALE, status=409)
            layer.data, layer.shape_count, layer.version = data, len(data['shapes']), layer.version + 1
            layer.save(update_fields=['data', 'shape_count', 'version', 'updated_at'])
    return _layer_json(request, project, layer, rev)


@require_POST
def markup_add(request, org_slug, pk, doc_id, rev_id):
    """Add shapes to this person's own layer on a revision: the compare page clouds a change this way."""
    project = get_project(request, pk)
    doc, rev = _markup_target(request, project, doc_id, rev_id)
    try:
        new = markup.parse_shapes(request.POST.get('shapes', ''), rev.page_count)
        with transaction.atomic():
            layer = MarkupLayer.objects.select_for_update().filter(revision=rev, owner=request.user).first()
            if layer is None:
                layer = MarkupLayer(project=project, document=doc, revision=rev, owner=request.user)
            elif layer.pk:
                layer.version += 1
            shapes = markup.clean_shapes(layer.shapes + new, rev.page_count)
            layer.data = markup.check_size({'v': markup.VERSION, 'shapes': shapes})
            layer.shape_count = len(shapes)
            with transaction.atomic():
                layer.save()
    except markup.InvalidMarkup as e:
        return _error(str(e))
    except IntegrityError:
        return _error(STALE, status=409)
    return _layer_json(request, project, layer, rev)


def markup_data(request, org_slug, pk, doc_id, layer_id):
    """One layer's shapes, as JSON, for someone who may see it."""
    project = get_project(request, pk)
    doc = get_object_or_404(visible_documents(request, project), pk=doc_id)
    layer = get_object_or_404(visible_layers(request, project, doc), pk=layer_id)
    resp = _layer_json(request, project, layer, None)
    resp['Cache-Control'] = 'private, no-store'
    return resp


def _back(request, project, doc, revision_id):
    nxt = request.POST.get('next', '')
    prefix = reverse('project_detail', args=[request.org.slug, project.pk])
    if nxt.startswith(prefix) and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                                                  require_https=request.is_secure()):
        return redirect(nxt)
    return redirect('drawing_viewer', org_slug=request.org.slug, pk=project.pk, doc_id=doc.pk, rev_id=revision_id)


def _own_layer(request, project, doc_id, layer_id):
    doc = get_object_or_404(visible_documents(request, project), pk=doc_id)
    layer = get_object_or_404(visible_layers(request, project, doc), pk=layer_id)
    if layer.owner_id != request.user.pk:
        raise PermissionDenied('Only the person who drew a mark-up can change who sees it.')
    return doc, layer


@require_POST
def markup_share(request, org_slug, pk, doc_id, layer_id):
    project = get_project(request, pk)
    doc, layer = _own_layer(request, project, doc_id, layer_id)
    choices = dict(share_choices(request))
    share = request.POST.get('share', '')
    if share not in choices:
        messages.error(request, 'Choose who can see this mark-up from the list.')
        return _back(request, project, doc, layer.revision_id)
    if share != layer.share:
        layer.share = share
        layer.save(update_fields=['share', 'updated_at'])
        if share == MarkupLayer.EXTERNAL:
            log_activity(request, 'shared a mark-up of', f'{doc.label} rev {layer.revision.revision}', project=project,
                         url=reverse('drawing_viewer', args=[org_slug, project.pk, doc.pk, layer.revision_id]),
                         internal_only=True)
    if share == MarkupLayer.PRIVATE:
        messages.success(request, f'Your mark-up on rev {layer.revision.revision} is private: only you can see it.')
    else:
        who = choices[share]
        messages.success(request, f'Your mark-up on rev {layer.revision.revision} is shared with '
                                  f'{who[0].lower()}{who[1:]}.')
    return _back(request, project, doc, layer.revision_id)


@require_POST
def markup_delete(request, org_slug, pk, doc_id, layer_id):
    project = get_project(request, pk)
    doc, layer = _own_layer(request, project, doc_id, layer_id)
    code, rev_id = layer.revision.revision, layer.revision_id
    layer.delete()
    messages.success(request, f'Your mark-up on rev {code} has been deleted. The drawing itself is unchanged.')
    return _back(request, project, doc, rev_id)


# ---------------------------------------------------------------------------
# Revision compare
# ---------------------------------------------------------------------------

def revision_order(revs):
    """Oldest first, by the same rule as the current revision: P/C codes ranked, else upload order."""
    if all(revision_rank(r.revision) for r in revs):
        return sorted(revs, key=lambda r: (revision_rank(r.revision), r.uploaded_at, r.pk))
    return sorted(revs, key=lambda r: (r.uploaded_at, r.pk))


def _pick(revs, value):
    value = (value or '').strip()
    if value.isdigit() and value.isascii():
        return next((r for r in revs if r.pk == int(value)), None)
    return None


def drawing_compare(request, org_slug, pk, doc_id):
    """Two revisions of a drawing side by side, or overlaid (old red, new blue, unchanged black)."""
    project = get_project(request, pk)
    doc = get_object_or_404(visible_documents(request, project), pk=doc_id)
    revs = revision_order(viewable_revisions(request, project, doc))
    annotate(request, revs, visible_ids(request, project))
    ctx = {'doc': doc, 'revs': revs}
    if len(revs) < 2:
        return render(request, 'viewer/compare.html', docs_context(request, project, 'register', **ctx))
    live = [r for r in revs if not r.is_withdrawn]
    new = _pick(revs, request.GET.get('b')) or display_revision(live or revs)
    old = _pick(revs, request.GET.get('a'))
    if old is None or old.pk == new.pk:
        earlier = revs[:revs.index(new)]
        old = earlier[-1] if earlier else next(r for r in revs if r.pk != new.pk)
    mine = MarkupLayer.objects.filter(revision=new, owner=request.user).first()
    can_cloud = can_mark_up(request, project, new)
    config = {
        'pdfjs': static('vendor/pdfjs/pdf.min.js'),
        'worker': static('vendor/pdfjs/pdf.worker.min.js'),
        'fonts': static('vendor/pdfjs/standard_fonts/'),
        'markup_js': static('viewer/markup.js'),
        'a': {'pdf': reverse('drawing_pdf', args=[org_slug, project.pk, doc.pk, old.pk]), 'label': old.revision},
        'b': {'pdf': reverse('drawing_pdf', args=[org_slug, project.pk, doc.pk, new.pk]), 'label': new.revision},
        'add_url': reverse('markup_add', args=[org_slug, project.pk, doc.pk, new.pk]) if can_cloud else '',
        'mine_url': reverse('markup_data', args=[org_slug, project.pk, doc.pk, mine.pk]) if mine else '',
    }
    ctx.update(old=old, new=new, config=config, can_cloud=can_cloud,
               viewer_new=reverse('drawing_viewer', args=[org_slug, project.pk, doc.pk, new.pk]))
    return render(request, 'viewer/compare.html', docs_context(request, project, 'register', **ctx))
