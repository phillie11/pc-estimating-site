"""
The drawing viewer: a revision drawn in the browser by pdf.js, with snag and RFI pins.

Every view fetches the project with projects.access.get_project() and the document and revision
through documents.access, so the viewer reaches nothing the person couldn't already download.
The PDF itself is sent by projects.files.serve(), with the same malware-scan gate and download
log as the download button.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_POST

from documents.access import annotate, visible_documents, visible_ids
from documents.views import docs_context
from projects.access import get_project, is_internal
from projects.files import serve
from tenancy.models import log_activity

from . import pins as pinlib
from .access import (best_revision, can_raise_snag, can_remove, can_view, get_doc_and_revision, is_pdf,
                     visible_issues, visible_pins, visible_revision_ids, visible_rfis)
from .models import InvalidPin, PinLocked


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
    current = rev.current_revision
    if current is not None and seen is not None and current.pk not in seen:
        current = None  # not issued to them: don't name it
    current_viewable = current if current is not None and can_view(request, current) else None

    internal = is_internal(request)
    can_pin = not project.is_archived and not rev.is_withdrawn
    rev_seen = visible_revision_ids(request, project)
    pins = [pinlib.describe(request, project, p, rev=rev, seen=rev_seen) for p in visible_pins(request, project, doc)]
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
        'choices': choices,
    }
    return render(request, 'viewer/viewer.html', docs_context(
        request, project, 'register', doc=doc, rev=rev, current=current, current_viewable=current_viewable,
        config=config, pins=pins, can_pin=can_pin, internal=internal,
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
