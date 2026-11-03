"""
Shared parts of the Quality views: the sub-navigation, CSV, photo files, the checklist library and
the company's quality settings. Every project view fetches the job with
quality.access.quality_project() (get_project() plus "staff on the job").
"""
import csv

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from levelling.exports import safe
from projects.files import serve
from projects.views import project_context
from tenancy.permissions import admin_required

from .. import photos as photo_files
from ..access import can_edit_library, is_pm_or_admin, quality_project, require_library_editor
from ..forms import SettingsForm, TemplateForm
from ..library import ensure_library
from ..models import ChecklistTemplate, Inspection, Penetration, QualityPhoto, QualitySettings, TemplateItem

PAGE_SIZE = 50
NAV = (('inspections', 'Inspections', 'inspection_list'), ('permits', 'Permits to work', 'permit_list'),
       ('firestopping', 'Fire-stopping', 'penetration_list'))


def ctx(request, project, sub, **extra):
    org, pk = request.org.slug, project.pk
    extra['quality_nav'] = [(code, label, reverse(name, args=[org, pk])) for code, label, name in NAV]
    extra['quality_sub'] = sub
    return project_context(request, project, 'quality', **extra)


def page_of(request, rows):
    return Paginator(rows, PAGE_SIZE).get_page(request.GET.get('page'))


def csv_response(filename, header, rows):
    """A CSV download where no cell can run as a formula in a spreadsheet (levelling.exports.safe)."""
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for row in rows:
        w.writerow([safe(v) for v in row])
    return resp


def first_error(form):
    for errors in form.errors.values():
        if errors:
            return errors[0]
    return 'Check the form and try again.'


# --- photos --------------------------------------------------------------------------

def photo_file(request, org_slug, pk, photo_id, size):
    project = quality_project(request, pk)
    if size not in photo_files.SIZES:
        raise Http404
    photo = get_object_or_404(QualityPhoto, project=project, pk=photo_id)
    field = {'thumb': photo.thumbnail, 'display': photo.display, 'original': photo.image}[size] or photo.image
    return serve(request, field, project=project, label=f'Quality photo {photo.pk} ({size})')


@require_POST
def photo_remove(request, org_slug, pk, photo_id):
    """Remove a photo added by mistake: whoever added it, or the PM or an admin, while the record is still open."""
    project = quality_project(request, pk)
    photo = get_object_or_404(QualityPhoto.objects.select_related('item__inspection', 'penetration'),
                              project=project, pk=photo_id)
    if not (photo.uploaded_by_id == request.user.pk or is_pm_or_admin(request.user, project, request.membership)):
        raise PermissionDenied
    if photo.item_id:
        record, back = photo.item.inspection, photo.item.inspection.get_absolute_url() + f'#check-{photo.item_id}'
        locked = record.status != Inspection.IN_PROGRESS
    else:
        record, back = photo.penetration, photo.penetration.get_absolute_url()
        locked = record.status == Penetration.SIGNED_OFF
    if locked:
        messages.error(request, f'{record.ref} is finished, so its photos are part of the record and stay. '
                                'Reopen it first if a photo was added by mistake.')
        return redirect(back)
    with transaction.atomic():
        photo.delete()
        record.save(actor=request.user, audit_action='photo removed', audit_note=str(photo)[:255])
    photo_files.delete_files(photo)
    messages.success(request, 'Photo removed.')
    return redirect(back)


# --- checklist library (company-wide) --------------------------------------------------

def template_list(request, org_slug):
    if request.membership is None or not request.membership.is_internal:
        raise PermissionDenied
    ensure_library(request.org, request.user)
    show = request.GET.get('show', '')
    qs = ChecklistTemplate.objects.all() if show == 'all' else ChecklistTemplate.objects.filter(is_active=True)
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(trade__icontains=q))
    templates = qs.annotate(n=Count('items')).order_by('trade', 'name')
    page = page_of(request, templates)
    return render(request, 'quality/template_list.html', {
        'page': page, 'templates': page.object_list, 'q': q, 'show': show, 'filtered': bool(q),
        'can_edit': can_edit_library(request)})


def _save_template(form, user, template=None):
    with transaction.atomic():
        t = form.save(commit=False)
        if template is None:
            t.created_by = user
        t.save()
        TemplateItem.objects.filter(template=t).delete()
        TemplateItem.objects.bulk_create([TemplateItem(organization=t.organization, template=t, order=i, text=text)
                                          for i, text in enumerate(form.cleaned_data['checks'])])
    return t


def template_create(request, org_slug):
    require_library_editor(request)
    form = TemplateForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        t = _save_template(form, request.user)
        messages.success(request, f'Checklist "{t.name}" added. It is ready to use on any job.')
        return redirect('quality_templates', org_slug=org_slug)
    return render(request, 'quality/template_form.html', {'form': form, 'template': None})


def template_edit(request, org_slug, template_id):
    if request.membership is None or not request.membership.is_internal:
        raise PermissionDenied
    template = get_object_or_404(ChecklistTemplate, pk=template_id)
    if request.method == 'POST':
        require_library_editor(request)
    form = TemplateForm(request.POST or None, instance=template)
    if not can_edit_library(request):
        for field in form.fields.values():
            field.disabled = True
    if request.method == 'POST' and form.is_valid():
        _save_template(form, request.user, template)
        messages.success(request, f'Checklist "{template.name}" saved. Inspections already started keep their checks.')
        return redirect('quality_templates', org_slug=org_slug)
    used = Inspection.objects.filter(template=template).count()
    return render(request, 'quality/template_form.html', {'form': form, 'template': template, 'used': used,
                                                          'can_edit': can_edit_library(request)})


@admin_required
def quality_settings(request, org_slug):
    obj = QualitySettings.for_org(request.org)
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Quality settings saved. They apply to permits asked for from now on.')
        return redirect('quality_settings', org_slug=org_slug)
    return render(request, 'quality/settings.html', {'form': form})
