"""Upload validation and permission-checked file serving."""
import mimetypes
import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404

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


def serve(fieldfile, download_name=None):
    if not fieldfile:
        raise Http404
    try:
        fh = fieldfile.open('rb')
    except FileNotFoundError:
        raise Http404
    name = download_name or os.path.basename(fieldfile.name)
    ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    inline = ctype in INLINE_TYPES
    resp = FileResponse(fh, as_attachment=not inline, filename=name, content_type=ctype)
    resp['X-Content-Type-Options'] = 'nosniff'
    resp['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    resp['Cache-Control'] = 'private, no-store'
    return resp
