"""
Pins on drawings: a snag/issue or an RFI marked at a point on a page of a document.

A pin belongs to the document, not to one revision, so it still shows when a newer revision is
uploaded; it remembers which revision it was placed on ("placed on rev P02"). The point is
stored as x, y from 0 to 1 across and down the page (0, 0 is the top left), so it lands in the
same place whatever size the page is drawn at.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import models
from django.db.models import Q

from projects.models import _private_path
from tenancy.models import TenantModel

COORD_PLACES = Decimal('0.000001')


def upload_markup_export(instance, filename):
    return _private_path(instance, filename, 'markup-exports')


class InvalidPin(ValueError):
    """A pin position that isn't on the page: shown to the person as it stands."""


class PinLocked(PermissionError):
    """The record is already pinned on this drawing by someone this person can't overrule."""
    message = 'This is already pinned by the site team; ask them to move it.'


def parse_coord(value):
    """
    A position across or down the page, from 0 to 1, as a Decimal to 6 places. Refuses anything
    else (blank, words, NaN, infinity, booleans, or a value off the page) with InvalidPin.
    """
    if isinstance(value, bool) or value is None:
        raise InvalidPin('The pin position must be a number from 0 to 1.')
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise InvalidPin('The pin position must be a number from 0 to 1.')
    if not d.is_finite():
        raise InvalidPin('The pin position must be a number from 0 to 1.')
    if d < 0 or d > 1:
        raise InvalidPin('The pin must be on the page (a position from 0 to 1).')
    return d.quantize(COORD_PLACES, rounding=ROUND_HALF_UP)


def parse_page(value, page_count=None):
    """A page number from 1, and no more than the revision's page count when that's known."""
    text = str(value if value is not None else '').strip()
    if not text.isdigit() or not text.isascii():
        raise InvalidPin('The page must be a whole number from 1.')
    page = int(text)
    if page < 1 or page > 10000 or (page_count and page > page_count):
        raise InvalidPin('That page is not in this drawing.')
    return page


class DrawingPin(TenantModel):
    KIND_ISSUE, KIND_RFI = 'issue', 'rfi'

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='drawing_pins')
    document = models.ForeignKey('projects.Document', on_delete=models.CASCADE, related_name='pins')
    # The revision it was placed on. Kept as text too, so the label survives the revision being deleted.
    revision = models.ForeignKey('projects.DocumentRevision', null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='+')
    revision_code = models.CharField(max_length=20, blank=True)
    page = models.PositiveIntegerField(default=1)
    x = models.DecimalField(max_digits=7, decimal_places=6, help_text='0 is the left edge of the page, 1 the right.')
    y = models.DecimalField(max_digits=7, decimal_places=6, help_text='0 is the top of the page, 1 the bottom.')
    issue = models.ForeignKey('projects.Issue', null=True, blank=True, on_delete=models.CASCADE,
                              related_name='drawing_pins')
    rfi = models.ForeignKey('rfis.RFI', null=True, blank=True, on_delete=models.CASCADE, related_name='drawing_pins')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['page', 'created_at', 'pk']
        constraints = [
            models.CheckConstraint(condition=Q(issue__isnull=False, rfi__isnull=True)
                                   | Q(issue__isnull=True, rfi__isnull=False), name='viewer_pin_one_record'),
            models.CheckConstraint(condition=Q(x__gte=0, x__lte=1, y__gte=0, y__lte=1), name='viewer_pin_on_page'),
            models.CheckConstraint(condition=Q(page__gte=1), name='viewer_pin_page_from_1'),
            # One pin per record per drawing (NULLs don't clash, so this works on MySQL too)
            models.UniqueConstraint(fields=['document', 'issue'], name='viewer_pin_one_per_issue'),
            models.UniqueConstraint(fields=['document', 'rfi'], name='viewer_pin_one_per_rfi'),
        ]

    def __str__(self):
        return f'{self.record} on {self.document} p{self.page}'

    @property
    def kind(self):
        return self.KIND_ISSUE if self.issue_id else self.KIND_RFI

    @property
    def record(self):
        return self.issue if self.issue_id else self.rfi


class MarkupLayer(TenantModel):
    """
    One person's mark-up on one revision of a drawing: pen, lines, arrows, boxes, clouds, notes and
    measurements, stored as vector shapes (viewer/markup.py) with positions as fractions of the page.
    It is never baked into the PDF, so a layer drawn on rev P02 can be shown over rev C01.

    Private to its owner until they share it: with the site team (our staff on the job), or, for
    staff layers, also with every external who can open the revision it was drawn on. Who sees what
    is decided in viewer.access.visible_layers.
    """
    PRIVATE, TEAM, EXTERNAL = 'PRIVATE', 'TEAM', 'EXTERNAL'
    SHARE_CHOICES = [(PRIVATE, 'Only me'), (TEAM, 'The site team'),
                     (EXTERNAL, 'The site team and everyone this drawing is shared with')]
    SHARE_TONES = {PRIVATE: 'muted', TEAM: 'info', EXTERNAL: 'accent'}

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='markup_layers')
    document = models.ForeignKey('projects.Document', on_delete=models.CASCADE, related_name='markup_layers')
    revision = models.ForeignKey('projects.DocumentRevision', on_delete=models.CASCADE, related_name='markup_layers')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    share = models.CharField(max_length=10, choices=SHARE_CHOICES, default=PRIVATE)
    data = models.JSONField(default=dict, blank=True, help_text='Checked by viewer.markup.parse before saving.')
    shape_count = models.PositiveIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['revision', 'owner'], name='viewer_markup_one_per_person_per_revision'),
            models.CheckConstraint(condition=Q(share__in=['PRIVATE', 'TEAM', 'EXTERNAL']), name='viewer_markup_share'),
        ]
        indexes = [models.Index(fields=['document', 'share'])]

    def __str__(self):
        return f'Mark-up by {self.owner} on {self.revision}'

    @property
    def shapes(self):
        return (self.data or {}).get('shapes') or []

    @property
    def share_tone(self):
        return self.SHARE_TONES.get(self.share, 'muted')


class MarkupExport(TenantModel):
    """
    A vector PDF of one revision with mark-ups drawn on it as real shapes and text (viewer/export.py), made by
    the `viewer.markup_export` job. It holds a copy of the shapes as they were when it was asked for, so what
    the person was allowed to see then is all that is drawn, and it is only ever downloaded by the person who
    asked, and only while they can still open that revision. One per person per revision: asking again
    replaces the last one.
    """
    QUEUED, READY, FAILED = 'QUEUED', 'READY', 'FAILED'
    STATUS_CHOICES = [(QUEUED, 'Being made'), (READY, 'Ready to download'), (FAILED, 'Could not be made')]

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='markup_exports')
    document = models.ForeignKey('projects.Document', on_delete=models.CASCADE, related_name='markup_exports')
    revision = models.ForeignKey('projects.DocumentRevision', on_delete=models.CASCADE, related_name='markup_exports')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    layer_ids = models.JSONField(default=list, blank=True)
    shapes = models.JSONField(default=list, blank=True, help_text='Checked by viewer.markup.clean_shapes.')
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=QUEUED)
    file = models.FileField(upload_to=upload_markup_export, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    pages = models.PositiveIntegerField(default=0)
    shapes_drawn = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['revision', 'requested_by'], name='viewer_export_one_per_person_per_rev'),
            models.CheckConstraint(condition=Q(status__in=['QUEUED', 'READY', 'FAILED']), name='viewer_export_status'),
        ]

    def __str__(self):
        return f'Mark-up export of {self.revision} for {self.requested_by}'
