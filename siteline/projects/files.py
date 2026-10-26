"""Upload validation and permission-checked file serving."""
import mimetypes
import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponseRedirect
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


def serve(request, fieldfile, download_name=None, project=None, label=''):
    """
    Send a file the caller has already checked the user may see, and log the download.

    On this server's disk the file is streamed back. In cloud storage the browser is
    redirected to a signed link that expires in a few minutes, so a forwarded link is
    useless and big drawing packs don't pass through the app server.
    """
    if not fieldfile:
        raise Http404
    name = download_name or os.path.basename(fieldfile.name)
    ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    inline = ctype in INLINE_TYPES
    _log_access(request, fieldfile, project, label or name)
    if settings.STORAGE_BUCKET:
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
