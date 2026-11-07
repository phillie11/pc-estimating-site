"""
Demo data for Quality, called by `manage.py seed_demo` inside the demo company's org_context, after
the Leeds job's packages, drawings (documents/demo.py) and snags exist.

  The company's checklist library (quality/library.py).
  Leeds bar job (project 1)
    Inspections  INS-001 partitions passed; INS-002 fire doors failed, one snag raised to the
                 joinery subcontractor and one failed check still without a snag; INS-003 ceilings
                 in progress, the PM's to finish today (Needs me).
    Permits      PTW-001 hot works closed with the fire watch; PTW-002 hot works for tomorrow waiting
                 for the PM's approval (Needs me); PTW-003 working at height running out within the
                 hour (Needs me, expiring); PTW-004 isolation that ran out yesterday and was never
                 closed (Needs me, expired); PTW-005 working at height for tomorrow, asked for by the
                 joinery subcontractor in their portal, waiting for the PM's approval (Needs me).
    Fire-stopping  FS-001 to FS-006: two signed off (one pinned on the ground floor GA), one sealed
                 waiting for the PM to inspect (Needs me), one failed back with the PM to get
                 resealed (Needs me), two still to seal. The first three were marked sealed by the
                 installing subcontractor (the PM signs off; whoever sealed it can't). FS-007 was recorded
                 by the joinery subcontractor in their portal and waits for the PM (Needs me).
  Salford Quays (project 4): one passed inspection, an active permit and two penetrations.

Photos are plain coloured placeholders (diary/demo.py), not real photos.
"""
import datetime
from decimal import Decimal

from django.core.files.base import ContentFile
from django.utils import timezone

from core.workflow import apply_transition
from diary.demo import placeholder
from directory.models import Package
from projects.models import Document, Issue, Project, ProjectMember

from . import photos as photo_files
from . import rules
from .library import ensure_library
from .models import ChecklistTemplate, Inspection, InspectionItem, Penetration, Permit

COLOURS = ['#6E7F8C', '#8B6F4E', '#5E7F66', '#7D6A8A', '#8C8A5E', '#5F7391']
GA_NUMBER = 'CLL-NLD-ZZ-00-DR-A-1001'


def _photo(project, user, caption, n, **target):
    f = ContentFile(placeholder(caption, COLOURS[n % len(COLOURS)], size=(1000, 750)), name=f'QA_{n:03d}.jpg')
    return photo_files.add_photo(project, user, f, **target)


def _inspection(project, template_name, area, inspector, actor, results, package=None, notes=None, due=None):
    """Start one from the library and answer its checks: results is a list (PASS/FAIL/NA or '') or one value."""
    template = ChecklistTemplate.objects.get(name=template_name)
    ins = Inspection(project=project, template=template, area=area, package=package,
                     title=f'{template.name}, {area}', ball_in_court_user=inspector,
                     due_date=due or timezone.localdate())
    ins.save(actor=actor)
    checks = list(template.items.all())
    if isinstance(results, str):
        results = [results] * len(checks)
    now = timezone.now()
    items = []
    for i, check in enumerate(checks):
        result = results[i] if i < len(results) else ''
        items.append(InspectionItem.objects.create(
            inspection=ins, order=check.order, text=check.text, result=result,
            note=(notes or {}).get(i, ''), answered_by=inspector if result else None,
            answered_at=now if result else None))
    return ins, items


def _finish(ins, user):
    outcome = rules.inspection_outcome(ins.items.values_list('result', flat=True))
    apply_transition(ins, 'pass' if outcome == 'PASSED' else 'fail', user, note='Demo')


def _sub_user(project, package):
    m = ProjectMember.objects.filter(project=project, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                     subcontractor=package.subcontractor).select_related('user').first()
    return m.user if m else None


def _permit(project, kind, title, location, asked_by, start, hours, holder='', package=None, notes=''):
    p = Permit(project=project, permit_type=kind, title=title, location=location, holder=holder, package=package,
               starts_at=start, expires_at=start + datetime.timedelta(hours=hours), method_notes=notes)
    from .workflow import permit_approver_for
    p.ball_in_court_user = permit_approver_for(project, asked_by)
    p.save(actor=asked_by)
    return p


def _approve(p, user):
    """Approve as the demo would on the day: the clock check in the workflow is for live use, so set it directly."""
    p.status = rules.APPROVED
    p.precautions = rules.precautions_for(p.permit_type)
    p.approved_by, p.approved_at = user, p.starts_at - datetime.timedelta(minutes=20)
    p.ball_in_court_user = p.created_by
    p.save(actor=user, audit_action='approve')


def _penetration(project, user, location, element, rating, service, size, system='', package=None, installer='',
                 n=0, before=False, after=False, seal=False, sign_off_by=None, fail_by=None, fail_note='',
                 detail='', sealed_days_ago=2, sealer=None):
    p = Penetration(project=project, location=location, element=element, fire_rating=rating, service=service,
                    size=size, system=system, detail_reference=detail, package=package, installer=installer)
    p.title = p.default_title()
    p.save(actor=user)
    if before:
        _photo(project, user, f'Before: {location}', n, penetration=p, stage='BEFORE')
    if after:
        _photo(project, user, f'Sealed: {location}', n + 1, penetration=p, stage='AFTER')
    if seal:
        # Signed off by someone other than whoever recorded the seal (QualitySettings.fs_independent_sign_off)
        apply_transition(p, 'seal', sealer or user,
                         installed_on=timezone.localdate() - datetime.timedelta(days=sealed_days_ago))
    if sign_off_by:
        apply_transition(p, 'sign_off', sign_off_by, remark='Collar and sealant as the manufacturer\'s detail.')
    if fail_by:
        apply_transition(p, 'fail', fail_by, note=fail_note[:255], remark=fail_note)
    return p


def seed(*, leeds, pm, owner):
    org = leeds.organization
    ensure_library(org, owner)
    today = timezone.localdate()
    now = timezone.localtime()
    pkg = {p.trade.name: p for p in Package.objects.filter(project=leeds, status=Package.APPOINTED)
           .select_related('trade', 'subcontractor') if p.trade}
    joinery, drylining = pkg.get('Joinery'), pkg.get('Drylining & partitions')

    # --- Leeds inspections ------------------------------------------------------------
    ins1, items1 = _inspection(leeds, 'Partitions: before boarding', 'Kitchen', pm, pm, rules.PASS, package=drylining,
                               due=today - datetime.timedelta(days=3))
    _photo(leeds, pm, 'Kitchen partition pattresses', 0, item=items1[4])
    _finish(ins1, pm)

    results = [rules.PASS, rules.FAIL, rules.PASS, rules.PASS, rules.FAIL, rules.PASS, rules.NA, rules.PASS]
    ins2, items2 = _inspection(leeds, 'Fire doors', 'Back of house corridor', pm, pm, results, package=joinery,
                               notes={1: 'Gap at the head of D04 is 6 mm.', 4: 'D02 closer does not latch from 30°.'},
                               due=today - datetime.timedelta(days=1))
    _photo(leeds, pm, 'D04 head gap', 2, item=items2[1])
    sub = _sub_user(leeds, joinery) if joinery else None
    snag = Issue.objects.create(project=leeds, title=f'Failed check: {items2[1].text}', issue_type='QUALITY',
                                description=f'Gap at the head of D04 is 6 mm.\n\nFrom inspection {ins2.ref} {ins2.title}.',
                                location='Back of house corridor', assignee=sub or pm, created_by=pm,
                                due_date=today + datetime.timedelta(days=5))
    items2[1].snag = snag
    items2[1].save(update_fields=['snag'])
    _finish(ins2, pm)

    ins3, _ = _inspection(leeds, 'Suspended ceilings', 'Bar area', pm, owner,
                          [rules.PASS, rules.PASS, rules.PASS, '', '', '', ''], package=drylining, due=today)

    # --- Leeds permits ---------------------------------------------------------------
    p1 = _permit(leeds, rules.HOT_WORKS, 'Weld steel brackets for the bar canopy', 'Bar, above the back bar',
                 pm, (now - datetime.timedelta(days=2)).replace(hour=8, minute=0), 8, holder='Site welder',
                 package=joinery)
    _approve(p1, owner)  # never by whoever asked for it
    apply_transition(p1, 'close', pm, note='Fire watch kept', remark='Area checked at 16:30, all clear.',
                     fire_watch_minutes=60)
    tomorrow = (now + datetime.timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    _permit(leeds, rules.HOT_WORKS, 'Braze copper pipework to the kitchen sinks', 'Kitchen, wash-up area', owner,
            tomorrow, 8, holder='Plumbing supervisor', notes='RAMS ref PL-04. Kitchen extract isolated by others.')
    # Runs out within the hour, but always later today (so it is "Expiring soon" and on today's Needs me)
    end_of_day = now.replace(hour=23, minute=59, second=0, microsecond=0)
    runs_out = min(now.replace(second=0, microsecond=0) + datetime.timedelta(minutes=50), end_of_day)
    p3 = _permit(leeds, rules.HEIGHT, 'Install feature pendants from a tower scaffold', 'Bar, high-level ceiling',
                 pm, runs_out - datetime.timedelta(hours=8), 8,
                 holder='Electrical supervisor', package=pkg.get('Electrical'))
    _approve(p3, owner)
    p4 = _permit(leeds, rules.ISOLATION, 'Isolate DB-K1 to extend kitchen small power', 'Kitchen, DB-K1', pm,
                 (now - datetime.timedelta(days=2)).replace(hour=7, minute=30), 34, holder='Electrical supervisor',
                 package=pkg.get('Electrical'))
    _approve(p4, owner)
    if sub is not None:
        # Asked for by the joinery subcontractor in their portal (quality/portal.py): on the PM's Needs me
        _permit(leeds, rules.HEIGHT, 'Fix the bar canopy soffit panels from a mobile tower', 'Ground floor bar, canopy',
                sub, tomorrow + datetime.timedelta(hours=1), 8, holder=sub.full_name, package=joinery,
                notes='Tower erected by our PASMA-trained joiners. RAMS JN-RAMS-03.')

    # --- Leeds fire-stopping ---------------------------------------------------------
    installer = drylining or joinery
    # The subcontractor marks their own seals done in the portal; the PM inspects and signs them off
    sealer = (_sub_user(leeds, installer) if installer else None) or owner
    fs1 = _penetration(leeds, pm, 'Ground floor, kitchen to corridor wall', 'WALL', 60, 'PIPE_PLASTIC', '110 mm',
                       'Intumescent pipe collar and acoustic sealant', package=installer, installer='Fire-stopping operative',
                       detail='Manufacturer detail PC-110', n=10, before=True, after=True, seal=True, sign_off_by=pm,
                       sealed_days_ago=6, sealer=sealer)
    doc = Document.objects.filter(project=leeds, reference=GA_NUMBER).first()
    if doc is not None:
        rev = doc.revisions.filter(revision='C01').first() or doc.revisions.first()
        if rev is not None:
            fs1.drawing, fs1.drawing_revision, fs1.pin_page = doc, rev, 1
            fs1.pin_x, fs1.pin_y = Decimal('0.610000'), Decimal('0.470000')
            fs1.save(actor=pm, audit_action='pinned on a drawing')
    _penetration(leeds, pm, 'Ground floor, riser 1', 'FLOOR', 120, 'CABLE_TRAY', '300 x 50 mm tray',
                 'Fire batt with coated cables', package=installer, n=12, before=True, after=True, seal=True,
                 sign_off_by=pm, sealed_days_ago=5, sealer=sealer)
    _penetration(leeds, pm, 'Ground floor, bar store ceiling', 'CEILING', 30, 'DUCT', '250 mm duct',
                 'Fire damper with intumescent mastic', package=installer, n=14, before=True, after=True, seal=True,
                 sealer=sealer)
    _penetration(leeds, pm, 'Ground floor, corridor C1 north wall', 'WALL', 60, 'MIXED', '150 x 100 mm opening',
                 'Intumescent sealant', package=installer, n=16, before=True, after=True, seal=True, fail_by=pm,
                 fail_note='Sealant depth short of the detail; gap visible behind the cables.')
    _penetration(leeds, pm, 'Ground floor, WC lobby wall', 'WALL', 60, 'PIPE_METAL', '54 mm copper', n=18,
                 before=True)
    _penetration(leeds, pm, 'Ground floor, kitchen ceiling void', 'CEILING', 30, 'CABLE', 'Cable bundle')
    if joinery is not None and sub is not None:
        # Recorded by the joinery subcontractor in their portal, with photos: waiting for the PM to sign it off
        _penetration(leeds, sub, 'Ground floor, bar back wall behind the back bar', 'WALL', 30, 'CABLE',
                     '20 mm cable bundle', 'Intumescent sealant', package=joinery, installer=sub.full_name,
                     n=24, before=True, after=True, seal=True, sealed_days_ago=1)

    # --- Salford ---------------------------------------------------------------------
    salford = Project.objects.filter(name='Office CAT A, Salford Quays').first()
    if salford is not None:
        s_pm = salford.manager or pm
        ins, _ = _inspection(salford, 'M&E first fix', 'Level 4 open plan', s_pm, s_pm, rules.PASS,
                             due=today - datetime.timedelta(days=2))
        _finish(ins, s_pm)
        sp = _permit(salford, rules.HEIGHT, 'Containment above the level 4 ceiling grid', 'Level 4, grid C-F', s_pm,
                     now.replace(hour=7, minute=30, second=0, microsecond=0) - datetime.timedelta(days=1), 72,
                     holder='Electrical supervisor')
        _approve(sp, owner)
        _penetration(salford, s_pm, 'Level 4, core wall by lift 2', 'WALL', 120, 'PIPE_METAL', '28 mm copper',
                     'Intumescent wrap and sealant', n=20, before=True, after=True, seal=True, sign_off_by=s_pm,
                     sealed_days_ago=3, sealer=owner if owner != s_pm else None)
        _penetration(salford, s_pm, 'Level 4, riser 2 floor', 'FLOOR', 120, 'MIXED', 'Mixed services', n=22,
                     before=True)
    return {'inspections': Inspection.objects.count(), 'permits': Permit.objects.count(),
            'penetrations': Penetration.objects.count()}
