"""
Demo data for the site diary, called by `manage.py seed_demo` inside the demo organisation's
org_context, after the Leeds job, its packages, RFIs and change events exist.

  Leeds bar job (project 1): an entry for each of the last 10 working days, written by the PM
      in the evening: labour from the appointed subcontractors plus our own people, a delivery,
      a visitor, a 3-hour delay (late information on the bar back veneer), an instruction linked
      to RFI-001, and placeholder photos (two shared with the client).
  Salford Quays (project 4) and Liverpool ONE: a few days each, with Salford's most recent
      working day left unwritten so the PM has a "Site diary missing" item in Needs me.

Photos are plain coloured placeholders with a caption, drawn with Pillow; no real photos.
"""
import datetime
import io

from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from directory.models import Package
from projects.models import Project
from rfis.models import RFI

from . import photos as photo_files
from . import rules
from .models import (DelayLine, DeliveryLine, DiaryEntry, InstructionLine, LabourLine, PlantLine, SitePhoto,
                     VisitorLine)

WORK = {'Joinery': 'Bar back carcasses', 'Drylining & partitions': 'Kitchen partitions'}
COLOURS = ['#7A8B99', '#B5835A', '#6E8B74', '#8C6E8B', '#A39171', '#5F7A99', '#996B5F', '#7F8F5A']
WEATHER = [('OVERCAST', 'SHOWERS'), ('DRY', 'DRY'), ('RAIN', 'OVERCAST'), ('SUNNY', 'SUNNY'), ('DRY', 'WIND'),
           ('OVERCAST', 'OVERCAST'), ('SHOWERS', 'RAIN'), ('DRY', 'SUNNY'), ('FOG', 'DRY'), ('OVERCAST', 'DRY')]


def placeholder(caption, colour, size=(1200, 900)):
    """A plain coloured JPEG with the caption written on it, standing in for a site photo."""
    img = Image.new('RGB', size, colour)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=56)
        small = ImageFont.load_default(size=30)
    except TypeError:  # Pillow before 10.1
        font = small = ImageFont.load_default()
    draw.rectangle([0, size[1] - 190, size[0], size[1]], fill='#1F2328')
    draw.text((40, size[1] - 160), caption[:38], fill='white', font=font)
    draw.text((40, size[1] - 80), 'Demo placeholder, not a real photo', fill='#C9CDD2', font=small)
    draw.rectangle([40, 40, 220, 120], outline='white', width=4)
    draw.text((62, 58), 'DEMO', fill='white', font=font)
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=80)
    return buf.getvalue()


def _photo(project, entry, day, caption, area, user, n, shared=False):
    f = ContentFile(placeholder(caption, COLOURS[n % len(COLOURS)]), name=f'IMG_{day:%Y%m%d}_{n:02d}.jpg')
    p = SitePhoto(project=project, entry=entry, caption=caption, area=area, taken_on=day, uploaded_by=user,
                  uploaded_at=_at(day, 15, n % 50))
    photo_files.attach(p, f)
    if shared:
        p.shared_with_client, p.shared_at, p.shared_by = True, _at(day, 18), user
    p.save()
    return p


def _at(day, hour, minute=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


def _working_days(today, count):
    days, day = [], today - rules.ONE_DAY
    while len(days) < count:
        if rules.is_working_day(day):
            days.append(day)
        day -= rules.ONE_DAY
    return days  # most recent first


def _entry(project, day, pm, n, **kw):
    am, pm_weather = WEATHER[n % len(WEATHER)]
    e = DiaryEntry(project=project, entry_date=day, weather_am=am, weather_pm=pm_weather, site_start='07:30',
                   site_finish='17:00', own_staff_count=2, own_staff='Site manager, general labourer', **kw)
    e.save(actor=pm)
    return e


def _backdate(entry, day):
    """Written that evening, so the lock window runs from the day itself (seed data only; skips the audit)."""
    DiaryEntry.objects.filter(pk=entry.pk).update(created_at=_at(day, 17, 30))
    for model in (LabourLine, PlantLine, DeliveryLine, VisitorLine, InstructionLine, DelayLine):
        model.objects.filter(entry=entry).update(created_at=_at(day, 16))


def seed(*, leeds, pm, today=None):
    today = today or timezone.localdate()
    appointed = list(Package.objects.filter(project=leeds, status=Package.APPOINTED).select_related('subcontractor',
                                                                                                    'trade'))
    rfi = RFI.objects.filter(project=leeds, title__startswith='Bar back panelling').first()
    days = _working_days(today, 10)
    photo_n = 0
    for n, day in enumerate(reversed(days)):  # oldest first
        e = _entry(leeds, day, pm, n, hs_notes='Toolbox talk: working near live neighbouring units.' if n == 0 else '',
                   notes='Strip-out complete to the bar area.' if n == 1 else '')
        for i, pkg in enumerate(appointed):
            LabourLine.objects.create(entry=e, package=pkg, headcount=[4, 3, 5, 2][(n + i) % 4],
                                      notes=WORK.get(pkg.trade.name if pkg.trade_id else '', ''), created_by=pm)
        if n % 3 == 0:
            LabourLine.objects.create(entry=e, name='Rapid Labour Ltd', trade='Labour', headcount=2,
                                      notes='Clearing strip-out waste', created_by=pm)
        if n in (2, 3, 4):
            PlantLine.objects.create(entry=e, description='Scissor lift (19ft)', supplier='Speedy Hire',
                                     notes='Arrived' if n == 2 else '', created_by=pm)
        if n == 3:
            DeliveryLine.objects.create(entry=e, supplier='British Gypsum (via CCF)', description='Plasterboard and studs '
                                        'for kitchen partitions, 2 pallets', arrived_at=datetime.time(8, 15),
                                        notes='Delivery note CCF-448812, complete', created_by=pm)
        if n == 5:
            VisitorLine.objects.create(entry=e, name='Hannah Cole', company='Neon Leisure Group',
                                       reason='Client walk-round of the bar area', arrived_at=datetime.time(10, 30),
                                       created_by=pm)
        if n == 6:
            DelayLine.objects.create(entry=e, cause='INFORMATION', hours_lost=3,
                                     description='Joiners stood down on the bar back for the morning: veneer species '
                                                 'still not confirmed (RFI-001).', trades_affected='Joinery',
                                     created_by=pm)
            InstructionLine.objects.create(entry=e, given_by='Hannah Cole (client)', rfi=rfi,
                                           description='Verbal: hold the bar back veneer until the eucalyptus sample is '
                                                       'approved. Written confirmation to follow.', created_by=pm)
        e.save(actor=pm, audit_action='lines added', audit_note='Seeded demo lines')
        shots = [('Bar back carcasses fixed', 'Ground floor bar'), ('Kitchen partitions first fix', 'First floor kitchen'),
                 ('Strip-out waste cleared', 'Rear yard')]
        if n in (1, 4, 6, 8, 9):
            for j, (caption, area) in enumerate(shots[: 2 if n != 9 else 3]):
                photo_n += 1
                _photo(leeds, e, day, caption, area, pm, photo_n, shared=(n == 8 and j == 0) or (n == 9 and j == 0))
        _backdate(e, day)

    # A photo on the job but not in the diary
    photo_n += 1
    _photo(leeds, None, days[2], 'Existing ceiling void before works', 'Ground floor bar', pm, photo_n)

    # Salford and Liverpool: a few days each; Salford's latest working day is left for "Needs me"
    # The days "Needs me" checks right now (rules.expected_days); Salford leaves out the most recent one
    due = rules.expected_days(timezone.localtime().replace(tzinfo=None))
    for name, keep in (('Office CAT A, Salford Quays', due[1:]), ('Retail unit, Liverpool ONE', due)):
        project = Project.objects.filter(name=name).first()
        if project is None:
            continue
        for n, day in enumerate(keep):
            e = _entry(project, day, pm, n + 3)
            LabourLine.objects.create(entry=e, name='Northern Partitions', trade='Drylining & partitions',
                                      headcount=4 + n, created_by=pm)
            e.save(actor=pm, audit_action='lines added', audit_note='Seeded demo lines')
            _backdate(e, day)
    return {'leeds_days': days}
