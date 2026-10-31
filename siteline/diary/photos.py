"""
Site photo uploads: check the file really is a picture, then make the two copies people see.

  original    kept exactly as uploaded (its camera date and details are evidence); staff only
  display     at most 1600px, turned the right way up, saved as JPEG with no camera or location data;
              what the gallery opens and the only full-size copy a client gets
  thumbnail   at most 480px, same treatment

Uploads go through projects.files.validate_upload (blocked types, size limit) first. Pillow
then has to open and verify it as a JPEG, PNG, WebP or GIF; anything else, a corrupt file, or a
picture so large it could be a decompression bomb, is refused with a plain message. Resizing
uses JPEG draft mode so a 12-megapixel phone photo takes a fraction of a second.
"""
import io
import os
import warnings

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from projects.files import validate_upload

ALLOWED_FORMATS = {'JPEG', 'MPO', 'PNG', 'WEBP', 'GIF'}  # MPO: many phones save JPEGs as multi-picture
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
MAX_PIXELS = 60_000_000
DISPLAY_SIZE = 1600
THUMB_SIZE = 480
MAX_FILES = 20


def validate_photo(f):
    """Raise ValidationError unless f is a picture we can safely open."""
    validate_upload(f)
    ext = os.path.splitext(f.name or '')[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(f'{f.name} is not a photo. Upload JPEG, PNG, WebP or GIF pictures.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            f.seek(0)
            img = Image.open(f)
            if img.format not in ALLOWED_FORMATS:
                raise ValidationError(f'{f.name} is not a JPEG, PNG, WebP or GIF picture.')
            if img.width * img.height > MAX_PIXELS:
                raise ValidationError(f'{f.name} is too large a picture. Use one under 60 megapixels.')
            img.verify()
    except ValidationError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError,
            SyntaxError, ValueError):
        raise ValidationError(f'{f.name} could not be read as a picture. It may be damaged; try taking it again.')
    finally:
        f.seek(0)


def _jpeg(img, size):
    copy = img.copy()
    copy.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    copy.save(buf, 'JPEG', quality=82, optimize=True)  # no exif= argument: camera and location data are dropped
    return buf.getvalue()


def derivatives(f):
    """(display bytes, thumbnail bytes, width, height) for a file that passed validate_photo."""
    f.seek(0)
    img = Image.open(f)
    if img.format in ('JPEG', 'MPO'):
        img.draft('RGB', (DISPLAY_SIZE, DISPLAY_SIZE))
    img = ImageOps.exif_transpose(img)
    width, height = img.size
    if img.mode not in ('RGB', 'L'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        rgba = img.convert('RGBA')
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    elif img.mode == 'L':
        img = img.convert('RGB')
    display, thumb = _jpeg(img, DISPLAY_SIZE), _jpeg(img, THUMB_SIZE)
    f.seek(0)
    return display, thumb, width, height


def attach(photo, f):
    """Fill a SitePhoto's three files from an uploaded file (call before photo.save())."""
    base = os.path.splitext(os.path.basename(f.name))[0][:80] or 'photo'
    display, thumb, width, height = derivatives(f)
    photo.original_name = os.path.basename(f.name)[:255]
    photo.image.save(os.path.basename(f.name), f, save=False)
    photo.display.save(f'{base}-display.jpg', ContentFile(display), save=False)
    photo.thumbnail.save(f'{base}-thumb.jpg', ContentFile(thumb), save=False)
    photo.width, photo.height = width, height
    return photo
