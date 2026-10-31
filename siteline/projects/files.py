"""Upload validation, malware-scan checks and permission-checked file serving."""
import mimetypes
import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header

BLOCKED_EXTENSIONS = {
    '.exe', '.bat', '.cmd', '.com', '.msi', '.dll', '.scr', '.ps1', '.vbs', '.js', '.jar',
    '.sh', '.php', '.py', '.html', '.htm', '.svg', '.xhtml', '.hta',
}
INLINE_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf'}


def validate_upload(f):
    if f is None:
        return
    ext = os.path.splitext(f.name)[1].lower()
    if ext in BLOCKED_EXTENSIONS:
        raise ValidationError(f'{ext} files are not allowed.')
    if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f'Files must be under {settings.MAX_UPLOAD_MB} MB.')


def serve(request, fieldfile, download_name=None, project=None, label='', through_app=False):
    """
    Send a file the caller has already checked the user may see, and log the download.

    On this server's disk the file is streamed back. In cloud storage the browser is
    redirected to a signed link that expires in a few minutes, so a forwarded link is
    useless and big drawing packs don't pass through the app server. through_app=True
    streams it from cloud storage through this site instead (the drawing viewer fetches
    the PDF from script, and the Content-Security-Policy only lets it connect to us).
    """
    if not fieldfile:
        raise Http404
    name = download_name or os.path.basename(fieldfile.name)
    ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    inline = ctype in INLINE_TYPES
    blocked = _scan_gate(request, fieldfile, project, label or name)
    if blocked is not None:
        return blocked
    _log_access(request, fieldfile, project, label or name)
    if settings.STORAGE_BUCKET and not through_app:
        url = fieldfile.storage.url(fieldfile.name, parameters={
            'ResponseContentType': ctype,
            'ResponseContentDisposition': content_disposition_header(not inline, name),
            'ResponseCacheControl': 'private, no-store',
        }, expire=settings.STORAGE_LINK_SECONDS)
        resp = HttpResponseRedirect(url)
        resp['Cache-Control'] = 'private, no-store'
        resp['Referrer-Policy'] = 'no-referrer'
        return resp
    try:
        fh = fieldfile.open('rb')
    except FileNotFoundError:
        raise Http404
    resp = FileResponse(fh, as_attachment=not inline, filename=name, content_type=ctype)
    resp['X-Content-Type-Options'] = 'nosniff'
    resp['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    resp['Cache-Control'] = 'private, no-store'
    return resp


def _log_access(request, fieldfile, project, label):
    from .models import FileAccess
    if getattr(request, 'org', None) is None:
        return
    ip = request.META.get('HTTP_X_REAL_IP') or request.META.get('REMOTE_ADDR')
    FileAccess.objects.create(organization=request.org, user=request.user, project=project, label=label[:255],
                              path=fieldfile.name[:500], ip=ip or None)


# ---------------------------------------------------------------------------
# Malware scanning (GuardDuty Malware Protection for S3)
# ---------------------------------------------------------------------------

SCAN_TAG = 'GuardDutyMalwareScanStatus'


def scanning_enabled():
    return bool(settings.STORAGE_BUCKET and settings.STORAGE_MALWARE_SCAN)


def fetch_scan_tag(fieldfile):
    """GuardDuty's verdict on a stored file: a status string, or None while it hasn't been scanned yet."""
    from botocore.exceptions import ClientError
    storage = fieldfile.storage
    try:
        tags = storage.connection.meta.client.get_object_tagging(Bucket=storage.bucket_name, Key=fieldfile.name)
    except ClientError as err:
        if err.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            raise Http404
        raise
    return next((t['Value'] for t in tags.get('TagSet', []) if t['Key'] == SCAN_TAG), None)


def scan_status(fieldfile, org):
    """Cached once final, so each clean file costs one lookup in its lifetime."""
    from .models import FileScan
    if not scanning_enabled():
        return FileScan.CLEAN
    cached = FileScan.all_objects.filter(path=fieldfile.name).first()
    if cached is not None:
        return cached.status
    status = fetch_scan_tag(fieldfile)
    if status in FileScan.FINAL:
        FileScan.all_objects.get_or_create(path=fieldfile.name, defaults={'status': status, 'organization': org})
    return status


def staff_for(project, org):
    """Who hears about a blocked file: the project's own staff, or the company's admins."""
    from tenancy.models import Membership
    from .models import ProjectMember
    people = []
    if project is not None:
        people = [project.manager, project.owner] + [
            m.user for m in ProjectMember.all_objects.filter(
                project=project, role__in=[r for r, _ in ProjectMember.INTERNAL_ROLE_CHOICES]).select_related('user')]
    people = [u for u in people if u]
    if not people:
        people = [m.user for m in Membership.objects.filter(organization=org, role__in=Membership.ADMIN_ROLES,
                                                             is_active=True).select_related('user')]
    return people


def alert_blocked(org, path, status, label, project=None, extra=()):
    """Tell staff (and anyone in extra) about an infected or unscannable file, once per file."""
    from tenancy import notify
    from .models import FileScan
    scan = FileScan.all_objects.filter(path=path).first()
    if scan is None or scan.alerted_at is not None:
        return
    scan.alerted_at = timezone.now()
    scan.save(update_fields=['alerted_at'])
    what = 'Infected file blocked' if status == FileScan.INFECTED else 'File blocked: it could not be virus-checked'
    url = project.get_absolute_url() if project is not None else reverse('dashboard', args=[org.slug])
    notify.alert(staff_for(project, org) + list(extra), f'{what}: {label}', url, project=project, org=org)


def _scan_gate(request, fieldfile, project, label):
    """None if the file may be handed out; otherwise the page to show instead."""
    from .models import FileScan
    if not scanning_enabled():
        return None
    status = scan_status(fieldfile, request.org)
    if status == FileScan.CLEAN:
        return None
    if status is None:
        return render(request, 'files/scanning.html', {'label': label, 'project': project}, status=409)
    alert_blocked(request.org, fieldfile.name, status, label, project)
    return render(request, 'files/blocked.html', {'label': label, 'project': project,
                                                  'infected': status == FileScan.INFECTED}, status=403)
