"""
Demo data for safety, called by `manage.py seed_demo` inside the demo organisation's org_context,
after the jobs and the site diary exist.

  Leeds bar job (project 1, JCT IC):
      CDM register complete: notifiable, F10 sent, client, principal designer and principal
      contractor, construction phase plan v1 and v2.
      INC-001  near miss three weeks ago, investigated and closed.
      INC-002  a labourer hurt stepping off the scissor lift nine days ago, off for 9 days, so
               RIDDOR over-7-day: due to HSE within 15 days and not yet reported (on the PM's Needs
               me), with one action open and one done.
      INC-003  a near miss reported this morning, waiting for the PM to investigate.
      Site sign-in: the PM, the joinery sub and their team, and a visitor on site today; yesterday's
      sign-ins, one with no sign-out.
  Salford Quays (project 4, NEC4): CDM register only part done (client recorded; no plan, not
      checked for notification), so the PM has "Construction phase plan not uploaded" on Needs me.
      One open near miss and three people on site.

Names are invented. Nothing here is a real person.
"""
import datetime

from django.core.files.base import ContentFile
from django.utils import timezone

from projects.models import Project

from . import rules
from .models import (CDMProfile, DutyHolder, Incident, IncidentAction, PhasePlan, SiteAttendance, SiteGate)


def _at(day, hour, minute=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


def _incident(project, pm, reporter, when, **kw):
    i = Incident(project=project, occurred_at=when, ball_in_court_user=pm,
                 due_date=rules.investigation_due(timezone.localdate(when)), **kw)
    i.save(actor=reporter)
    Incident.objects.filter(pk=i.pk).update(created_at=when + datetime.timedelta(minutes=20))  # seed only
    return Incident.objects.get(pk=i.pk)


def _sign_in(project, day, hour, minute, name, company, kind, user=None, out=None, by=None):
    return SiteAttendance.objects.create(
        project=project, user=user, name=name, company=company, kind=kind,
        method=SiteAttendance.METHOD_MANUAL if by else SiteAttendance.METHOD_QR, signed_in_by=by,
        signed_in_at=_at(day, hour, minute), signed_out_at=_at(day, *out) if out else None)


def _leeds(leeds, pm, sub_user, org, demo_pdf, today, now):
    start = leeds.start_date or today - datetime.timedelta(days=28)
    cdm = CDMProfile.for_project(leeds)
    cdm.notifiable, cdm.working_days, cdm.peak_workers, cdm.person_days = 'YES', 52, 24, 760
    cdm.f10_reference, cdm.f10_submitted_on = 'F10/2026/DEMO-0412', start - datetime.timedelta(days=21)
    cdm.pci_received_on = start - datetime.timedelta(days=35)
    cdm.hs_file_due_on = (leeds.completion_date or today + datetime.timedelta(days=45)) + datetime.timedelta(days=28)
    cdm.updated_by, cdm.updated_at = pm, now - datetime.timedelta(days=20)
    cdm.save()
    for role, company, contact, appointed in [
        (DutyHolder.CLIENT, 'Neon Leisure Group', 'Hannah Cole', None),
        (DutyHolder.PD, 'Arc Interiors Design Studio', 'Design team lead', start - datetime.timedelta(days=40)),
        (DutyHolder.PC, org.name, pm.full_name, start - datetime.timedelta(days=30)),
        ('DESIGNER', 'Lund Joinery Ltd', 'Joinery drawing office', None),
    ]:
        DutyHolder.objects.create(project=leeds, role=role, company=company, contact_name=contact, appointed_on=appointed)
    for version, days, note in [(1, -14, 'Issued before start on site'), (2, 10, 'Updated for the first floor kitchen works')]:
        f = ContentFile(demo_pdf('Construction phase plan', f'Version {version}'), name=f'CPP-v{version}.pdf')
        PhasePlan.objects.create(project=leeds, version=version, file=f, original_name=f'Construction phase plan v{version}.pdf',
                                 size=f.size, note=note, uploaded_by=pm, uploaded_at=_at(start + datetime.timedelta(days=days), 9))

    d1 = today - datetime.timedelta(days=21)
    i1 = _incident(leeds, pm, pm, _at(d1, 10, 40),
                   description='Plasterboard sheet slid off the trolley by the goods lift and landed where a labourer '
                               'had been standing a moment before. Nobody hurt.',
                   location='Ground floor, goods lift lobby', kind=Incident.KIND_NEAR_MISS,
                   immediate_action='Area cleared, boards restacked flat on the trolley.',
                   root_cause='Boards stacked on edge on a trolley without a side rail.',
                   findings='Boards only move on the A-frame trolley with the rail fitted. Covered in the next '
                            'toolbox talk; drylining sub briefed.')
    a = IncidentAction(incident=i1, description='Toolbox talk on moving board materials', owner=pm,
                       due_date=d1 + datetime.timedelta(days=3), done_at=_at(d1 + datetime.timedelta(days=2), 8), done_by=pm,
                       created_by=pm)
    a.save()
    i1.save(actor=pm, audit_action='action added', audit_note=a.description)
    from core.workflow import apply_transition
    apply_transition(i1, 'investigate', pm)
    apply_transition(i1, 'close', pm)

    d2 = today - datetime.timedelta(days=9)
    i2 = _incident(leeds, pm, pm, _at(d2, 14, 15),
                   description='Labourer twisted his ankle stepping down off the scissor lift platform.',
                   location='Ground floor bar', kind=Incident.KIND_INJURY, anyone_hurt=True,
                   injured_name='Site labourer (Rapid Labour Ltd)', injured_company='Rapid Labour Ltd',
                   injury_details='Sprained right ankle. X-ray at hospital found no fracture.',
                   first_aid_given=True, taken_to_hospital=True, days_off=9,
                   riddor_category=rules.RIDDOR_OVER_7,
                   immediate_action='First aid on site, taken to hospital by the site manager. Lift isolated.')
    apply_transition(i2, 'investigate', pm)
    for text, due, done in [('Toolbox talk: getting on and off MEWPs, three points of contact', 2, False),
                            ('Check the scissor lift gate latch with the hire company', -6, True)]:
        a = IncidentAction(incident=i2, description=text, owner=pm, due_date=today + datetime.timedelta(days=due),
                           done_at=_at(today - datetime.timedelta(days=5), 11) if done else None,
                           done_by=pm if done else None, created_by=pm)
        a.save()
        i2.save(actor=pm, audit_action='action added', audit_note=text)

    _incident(leeds, pm, pm, now - datetime.timedelta(hours=2),
              description='Extension lead trailing across the kitchen doorway. Spotted before anyone tripped.',
              location='First floor kitchen entrance', kind=Incident.KIND_NEAR_MISS)

    gate = SiteGate.for_project(leeds)
    S, C, V = SiteAttendance.KIND_STAFF, SiteAttendance.KIND_COLLABORATOR, SiteAttendance.KIND_VISITOR
    yesterday = today - datetime.timedelta(days=1)
    _sign_in(leeds, yesterday, 7, 31, pm.full_name, org.name, S, user=pm, out=(17, 5))
    _sign_in(leeds, yesterday, 7, 44, sub_user.full_name, 'Lund Joinery Ltd', C, user=sub_user, out=(16, 30))
    _sign_in(leeds, yesterday, 7, 50, 'Tom Reid', 'Lund Joinery Ltd', V, out=(16, 32))
    _sign_in(leeds, yesterday, 8, 5, 'Kofi Mensah', 'Pennine Drylining', V)  # forgot to sign out
    _sign_in(leeds, yesterday, 13, 0, 'Fire risk assessor', 'Northern Fire Safety', V, out=(14, 10))
    if now.hour >= 7:
        _sign_in(leeds, today, 7, 32, pm.full_name, org.name, S, user=pm)
        _sign_in(leeds, today, 7, 45, sub_user.full_name, 'Lund Joinery Ltd', C, user=sub_user)
        _sign_in(leeds, today, 7, 48, 'Tom Reid', 'Lund Joinery Ltd', V)
        _sign_in(leeds, today, 7, 52, 'Sam Patel', 'Lund Joinery Ltd', V, by=pm)
        _sign_in(leeds, today, 8, 2, 'Kofi Mensah', 'Pennine Drylining', V)
        _sign_in(leeds, today, 8, 3, 'Ellie Brooks', 'Pennine Drylining', V)
    return gate


def _salford(salford, pm, org, today, now):
    DutyHolder.objects.create(project=salford, role=DutyHolder.CLIENT, company=salford.client_name or 'Arden Property')
    _incident(salford, pm, pm, now - datetime.timedelta(days=1, hours=3),
              description='Ceiling tile dropped from the grid while the M&E team were working above it.',
              location='Level 3, open plan office', kind=Incident.KIND_NEAR_MISS)
    SiteGate.for_project(salford)
    if now.hour >= 7:
        _sign_in(salford, today, 7, 40, 'Site manager', org.name, SiteAttendance.KIND_STAFF)
        _sign_in(salford, today, 7, 55, 'Ravi Shah', 'Northern Partitions', SiteAttendance.KIND_VISITOR)
        _sign_in(salford, today, 8, 10, 'Jo Walsh', 'Northern Partitions', SiteAttendance.KIND_VISITOR)


def seed(*, leeds, pm, sub_user, demo_pdf, today=None):
    today = today or timezone.localdate()
    now = timezone.localtime()
    org = leeds.organization
    gate = _leeds(leeds, pm, sub_user, org, demo_pdf, today, now)
    salford = Project.objects.filter(name='Office CAT A, Salford Quays').first()
    if salford is not None:
        _salford(salford, pm, org, today, now)
    return {'leeds_gate': gate}
