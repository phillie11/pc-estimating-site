"""
Photos on an inspection check or a fire-stopping penetration. The checking and resizing are the
site diary's (diary/photos.py): only real pictures are accepted, the original is kept for staff as
evidence, and the copies people see are turned upright and have camera and location data removed.
"""
from diary import photos as diary_photos

from .models import QualityPhoto

validate_photo = diary_photos.validate_photo
SIZES = ('thumb', 'display', 'original')


def add_photo(project, user, f, item=None, penetration=None, stage=''):
    """Store one uploaded (already validated) picture against a check or a penetration."""
    photo = QualityPhoto(project=project, item=item, penetration=penetration, stage=stage, uploaded_by=user)
    diary_photos.attach(photo, f)
    try:
        photo.save()
    except BaseException:
        diary_photos.delete_files(photo)
        raise
    return photo


def delete_files(photo):
    diary_photos.delete_files(photo)
