"""
Site photo uploads: check the file really is a picture, then make the two copies people see.

  original    kept exactly as uploaded (its camera date and details are evidence); staff only
  display     at most 1600px, turned the right way up, saved as JPEG with no camera, location or
              comment data; what the gallery opens and the only full-size copy a client gets
  thumbnail   at most 480px, same treatment

validate_photo() runs projects.files.validate_upload (blocked types, size limit), checks the
header (format, pixel count: 60 MP for JPEG, 40 MP for PNG and GIF, which can't be decoded at a
reduced size), then makes the derivatives there and then, so a truncated or corrupt file is a
form error rather than a server error. The result is kept on the file object for attach().

Memory: the picture is shrunk before anything else is done to it. JPEGs decode straight to about
display size (draft mode); PNG, GIF and WebP decode once at full size in their own mode and are
reduced in place (thumbnail()) before turning upright (_upright) and converting to RGB (_to_rgb),
so no full-size rotated or converted copy is ever made.
"""
import io
import os
import warnings

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from projects.files import validate_upload

ALLOWED_FORMATS = {'JPEG', 'MPO', 'PNG', 'WEBP', 'GIF'}  # MPO: many phones save JPEGs as multi-picture
DRAFT_FORMATS = {'JPEG', 'MPO'}  # decodable at a reduced size
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
MAX_PIXELS = 60_000_000          # JPEG (decoded reduced, so cheap)
MAX_PIXELS_FULL_DECODE = 40_000_000  # PNG, GIF, WebP: decoded at full size once
DISPLAY_SIZE = 1600
THUMB_SIZE = 480
MAX_FILES = 20
ROTATED = (5, 6, 7, 8)  # EXIF orientations that swap width and height

DECODE_ERRORS = (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError,
                 SyntaxError, ValueError, EOFError)


def validate_photo(f):
    """Raise ValidationError unless f is a picture we can fully decode; keeps the derivatives on f."""
    validate_upload(f)
    ext = os.path.splitext(f.name or '')[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(f'{f.name} is not a photo. Upload JPEG, PNG, WebP or GIF pictures.')
    unreadable = ValidationError(f'{f.name} could not be read as a picture. It may be damaged or only partly '
                                 'uploaded; try taking it again.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            f.seek(0)
            img = Image.open(f)
            if img.format not in ALLOWED_FORMATS:
                raise ValidationError(f'{f.name} is not a JPEG, PNG, WebP or GIF picture.')
            limit = MAX_PIXELS if img.format in DRAFT_FORMATS else MAX_PIXELS_FULL_DECODE
            if img.width * img.height > limit:
                raise ValidationError(f'{f.name} is too large a picture ({img.width} x {img.height}). '
                                      f'Use one under {limit // 1_000_000} megapixels.')
            f.diary_derivatives = derivatives(f)  # a full decode: truncated or corrupt files fail here
    except ValidationError:
        raise
    except DECODE_ERRORS:
        raise unreadable
    finally:
        f.seek(0)


def _shrink(img, size):
    """Reduce in place to fit size x size, in the image's own mode (no converted full-size copy)."""
    if img.format in DRAFT_FORMATS:
        img.draft('RGB', (size, size))
    img.thumbnail((size, size), Image.LANCZOS if img.mode not in ('P', '1') else Image.NEAREST)
    return img


def _upright(img, orientation):
    """Turn a (small) picture the right way up from its EXIF orientation."""
    if orientation in (2, 3, 4, 5, 6, 7, 8):
        img = ImageOps.exif_transpose(img)
    return img


def _to_rgb(img):
    """RGB on white, for JPEG. Only ever called on an already reduced picture."""
    if img.mode == 'RGB':
        return img
    if img.mode in ('RGBA', 'LA', 'P', 'PA') or 'transparency' in img.info:
        rgba = img.convert('RGBA')
        background = Image.new('RGB', img.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert('RGB')


def _jpeg(img, size):
    copy = img.copy() if max(img.size) > size else img
    copy.thumbnail((size, size), Image.LANCZOS)
    icc = img.info.get('icc_profile')
    copy.info = {}  # no comment, EXIF or XMP carried over from the camera
    buf = io.BytesIO()
    kwargs = {'icc_profile': icc} if icc else {}
    copy.save(buf, 'JPEG', quality=82, optimize=True, **kwargs)
    return buf.getvalue()


def derivatives(f):
    """(display bytes, thumbnail bytes, original width, original height). Fully decodes the picture."""
    f.seek(0)
    img = Image.open(f)
    orientation = img.getexif().get(0x0112, 1)
    width, height = img.size
    if orientation in ROTATED:
        width, height = height, width
    img = _shrink(img, DISPLAY_SIZE)
    icc = img.info.get('icc_profile')
    img = _to_rgb(_upright(img, orientation))
    if icc:
        img.info['icc_profile'] = icc
    display, thumb = _jpeg(img, DISPLAY_SIZE), _jpeg(img, THUMB_SIZE)
    f.seek(0)
    return display, thumb, width, height


def attach(photo, f):
    """Fill a SitePhoto's three files from an uploaded file (call before photo.save()). Returns the names written."""
    base = os.path.splitext(os.path.basename(f.name))[0][:80] or 'photo'
    display, thumb, width, height = getattr(f, 'diary_derivatives', None) or derivatives(f)
    photo.original_name = os.path.basename(f.name)[:255]
    photo.image.save(os.path.basename(f.name), f, save=False)
    photo.display.save(f'{base}-display.jpg', ContentFile(display), save=False)
    photo.thumbnail.save(f'{base}-thumb.jpg', ContentFile(thumb), save=False)
    photo.width, photo.height = width, height
    return photo


def delete_files(photo):
    """Remove a photo's stored files (a failed upload, or a deleted photo)."""
    for field in (photo.image, photo.display, photo.thumbnail):
        if field:
            try:
                field.delete(save=False)
            except Exception:  # nosec B110 - best effort: a missing file mustn't stop the clean-up
                pass
