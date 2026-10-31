"""
Demo data for procurement, called by `manage.py seed_demo` inside the demo organisation's
org_context, after the Leeds job, its packages and contracts exist.

  Leeds bar job: eight schedule lines (joinery, bar back, kitchen extract, ceilings, lighting,
      flooring, M&E, signage). The kitchen extract is red (approved but not ordered, order-by
      date passed); the bar back and the ceilings are amber. Lund Joinery (sub@demo.test) has
      one submittal to make (back-of-house joinery samples) and one at C needing revision 01
      (bar back shop drawings). The ceilings product data waits on the project manager's review,
      and the feature pendant samples are with the designer (designer@demo.test) for comment.
  Salford Quays office: three lines, one amber, one ordered.

Dates are set relative to today, so the RAG colours hold whenever the demo is seeded.
"""
from datetime import timedelta

from django.core.files.base import ContentFile
from django.utils import timezone

from core.workflow import apply_transition
from directory.models import Package
from projects.models import Project, ProjectMember
from tenancy.models import Membership

from .models import ScheduleLine, Submittal, SubmittalFile, SubmittalForward, SubmittalRevision


def _line(project, title, order_in_days, lead, approval, user, **kw):
    """A line whose order-by date lands about `order_in_days` from today (moved back to a working day)."""
    today = timezone.localdate()
    ros = today + timedelta(days=order_in_days + lead * 7)
    line = ScheduleLine(project=project, title=title, required_on_site=ros, lead_time_weeks=lead,
                        approval_weeks=approval, created_by=user, **kw)
    line.save()
    return line


def _revision(s, number, user, days_ago, note, demo_pdf, filename):
    rev = SubmittalRevision.objects.create(submittal=s, number=number, note=note, submitted_by=user,
                                           submitted_at=timezone.now() - timedelta(days=days_ago))
    content = ContentFile(demo_pdf(s.title, f'{s.ref} revision {rev.label}'), name=filename)
    SubmittalFile.objects.create(revision=rev, file=content, original_name=filename, size=content.size,
                                 uploaded_by=user)
    s.revision = number
    return rev


def seed(*, leeds, pm, sub_user, make_user, demo_pdf):
    today = timezone.localdate()
    org = leeds.organization
    designer = make_user('designer@demo.test', 'Ruth Okafor', 'Interior Designer')
    Membership.objects.get_or_create(organization=org, user=designer, defaults={'role': Membership.ROLE_EXTERNAL})
    ProjectMember.objects.get_or_create(project=leeds, user=designer, defaults={
        'role': ProjectMember.ROLE_CONSULTANT, 'company_name': 'Okafor Design Studio', 'added_by': pm})

    pkg = {p.trade.name: p for p in Package.objects.filter(project=leeds).select_related('trade') if p.trade}
    joinery, drylining = pkg['Joinery'], pkg['Drylining & partitions']
    electrical, flooring = pkg['Electrical'], pkg['Flooring']
    S = ScheduleLine

    bar_back = _line(leeds, 'Bar back and bar front', 5, 8, 2, pm, package=joinery, long_lead=True, status=S.APPOINTED,
                     design_info_by=today - timedelta(days=40),
                     notes='Veneer species still subject to RFI-001.')
    joinery_line = _line(leeds, 'Joinery: back of house, banquettes and bulkheads', 30, 6, 2, pm, package=joinery,
                         status=S.APPOINTED)
    extract = _line(leeds, 'Kitchen extract canopy and ductwork', -6, 10, 2, pm, long_lead=True, status=S.APPROVED,
                    supplier='Apex Mechanical (direct order)',
                    notes='Approved. Order held for the duct reroute (CE-002); chase the price today.')
    ceilings = _line(leeds, 'Suspended ceilings', 11, 3, 1, pm, package=drylining, status=S.APPOINTED)
    lighting = _line(leeds, 'Feature lighting', 45, 12, 2, pm, long_lead=True, status=S.APPOINTED,
                     supplier='Lumen Lab (client nominated)')
    _line(leeds, 'Flooring: resin to kitchen, timber to bar', 40, 4, 2, pm, package=flooring, status=S.TENDER)
    mande = _line(leeds, 'M&E: distribution boards and small power', -20, 2, 2, pm, package=electrical,
                  status=S.ORDERED)
    mande.ordered_on = today - timedelta(days=24)
    mande.save(update_fields=['ordered_on'])
    _line(leeds, 'Signage and manifestation', 60, 5, 2, pm, status=S.NOT_STARTED,
          supplier="Client's brand supplier", design_info_by=today + timedelta(days=55),
          notes='Artwork comes from the client; design information is later than our submittals date.')

    # SUB-001: bar back shop drawings, rev 00 given a C; Lund owe revision 01
    s1 = Submittal(project=leeds, line=bar_back, package=joinery, title='Bar back and bar front shop drawings',
                   kind=Submittal.SHOP_DRAWING, spec_reference='1203-JD-04 rev C',
                   description='General arrangement, sections and veneer setting-out for the bar back and front.',
                   ball_in_court_company=joinery.subcontractor, due_date=bar_back.submittals_due)
    s1.save(actor=pm)
    rev = _revision(s1, 0, sub_user, 12, 'First issue for comment.', demo_pdf, 'LJ-1203-SD-01-00.pdf')
    apply_transition(s1, 'submit', sub_user)
    rev.code, rev.reviewed_by, rev.reviewed_at = 'C', pm, timezone.now() - timedelta(days=4)
    rev.review_comments = ('Bar front panel joints must line up with the back bar shelving. Show the LED strip '
                           'channel and access for the drivers. Veneer to follow the RFI-001 answer.')
    rev.save()
    apply_transition(s1, 'reject', pm, note='Revise and resubmit')
    s1.due_date = today + timedelta(days=3)
    s1.save(actor=pm, audit_action='reassigned')
    bar_back.advance_to(S.IN_REVIEW)

    # SUB-002: back-of-house joinery samples, still to make
    s2 = Submittal(project=leeds, line=joinery_line, package=joinery, title='Back-of-house joinery samples',
                   kind=Submittal.SAMPLE, description='Laminate and edging samples for the back-of-house units, '
                                                      'and a banquette upholstery sample.',
                   ball_in_court_company=joinery.subcontractor, due_date=joinery_line.submittals_due)
    s2.save(actor=pm)

    # SUB-003: extract canopy product data, approved (A); the line is approved but not ordered, so red
    s3 = Submittal(project=leeds, line=extract, title='Extract canopy product data', kind=Submittal.PRODUCT_DATA,
                   ball_in_court_user=pm)
    s3.save(actor=pm)
    rev = _revision(s3, 0, pm, 20, 'Canopy and fan data from Apex.', demo_pdf, 'Apex-canopy-data.pdf')
    apply_transition(s3, 'submit', pm)
    rev.code, rev.reviewed_by, rev.reviewed_at = 'A', pm, timezone.now() - timedelta(days=15)
    rev.save()
    apply_transition(s3, 'approve', pm)

    # SUB-004: ceiling product data, in review with the project manager
    s4 = Submittal(project=leeds, line=ceilings, package=drylining, title='MF ceiling and acoustic tile data',
                   kind=Submittal.PRODUCT_DATA, ball_in_court_company=drylining.subcontractor)
    s4.save(actor=pm)
    _revision(s4, 0, pm, 2, 'Sent in by Pennine Drylining by email; uploaded for them.', demo_pdf,
              'PD-ceiling-data.pdf')
    apply_transition(s4, 'submit', pm)
    ceilings.advance_to(S.IN_REVIEW)

    # SUB-005: feature pendant samples, with the designer for comment
    s5 = Submittal(project=leeds, line=lighting, title='Feature pendant samples', kind=Submittal.SAMPLE,
                   ball_in_court_user=pm, description='Two pendant samples with finish swatches, for the designer.')
    s5.save(actor=pm)
    rev = _revision(s5, 0, pm, 5, 'Samples on site in the office; photos attached.', demo_pdf, 'pendant-photos.pdf')
    apply_transition(s5, 'submit', pm)
    SubmittalForward.objects.create(revision=rev, consultant=designer, forwarded_by=pm,
                                    forwarded_at=timezone.now() - timedelta(days=4),
                                    note='Please confirm the finish matches your scheme before we give it a status.')
    apply_transition(s5, 'forward', pm, consultant=designer)
    lighting.advance_to(S.IN_REVIEW)

    # Salford Quays: a lighter schedule
    salford = Project.objects.filter(name='Office CAT A, Salford Quays').first()
    if salford is not None:
        _line(salford, 'Raised access floor', 9, 6, 2, pm, long_lead=True, status=S.APPOINTED,
              supplier='Floor sub (Z1 contractor design)')
        _line(salford, 'Glazed partitions', 35, 8, 2, pm, long_lead=True, status=S.TENDER, supplier='Out to tender')
        ordered = _line(salford, 'Tea point appliances', -10, 3, 1, pm, status=S.ORDERED, supplier='Direct order')
        ordered.ordered_on = today - timedelta(days=12)
        ordered.save(update_fields=['ordered_on'])
    return {'designer': designer}
