"""
Demo data for close-out, called by `manage.py seed_demo` inside the demo organisation's
org_context, after the Leeds and Salford jobs, their packages, contracts and procurement exist.

  Leeds bar job (JCT IC, live): the ground floor bar was handed over 20 days ago (sectional
      completion, 12-month Rectification Period from the contract). The client (client@demo.test)
      has reported three defects in the bar: one waiting for the PM to give out, one with Lund
      Joinery (sub@demo.test) to fix, one fixed and waiting for the PM to check. The O&M manual is
      set up from the template: Lund's joinery O&M is accepted, the partitions O&M is still with
      the drylining subcontractor, and we've added our own H&S file notes. Two certificates.
      Version 1 of the manual is compiled and shared with the client, and handover pack 1 (the
      bar) is issued, so the client sees both under "Defects and handover".
  Salford Quays office (NEC4, live): O&M manual set up, nothing asked for yet, so the PM has the
      "ask for it" step to do; completion not recorded.
  Restaurant, Covent Garden (complete): practical completion five and a half months ago with a
      6-month defects period, so its end is on the PM's Needs me.
"""
from datetime import timedelta

from django.core.files.base import ContentFile
from django.utils import timezone

from core.workflow import apply_transition
from procurement.models import SubmittalFile, SubmittalRevision
from projects.models import Issue, Project

from . import services
from .models import Certificate, CompletionSection, OMDocument, OMSection


def _pdf(demo_pdf, title, sub, name):
    return ContentFile(demo_pdf(title, sub), name=name)


def seed(*, leeds, pm, client, sub_user, demo_pdf):
    today = timezone.localdate()
    org = leeds.organization

    # --- Leeds: sectional completion of the bar, defects from the client ------------------------
    bar = CompletionSection(project=leeds, name='Ground floor bar', is_whole_works=False,
                            pc_date=today - timedelta(days=20), notes='Bar handed over for the soft opening.')
    bar.certificate.save('Sectional-completion-bar.pdf', _pdf(demo_pdf, 'Certificate of sectional completion',
                                                            'Section 1: ground floor bar', 'sc1.pdf'), save=False)
    services.set_completion(bar, pm)
    services.set_completion(CompletionSection(project=leeds, name='First floor kitchen', is_whole_works=False), pm)

    def defect(title, location, days_ago, **kw):
        i = Issue(project=leeds, title=title, location=location, issue_type='DEFECT', created_by=client, **kw)
        i.save()
        Issue.objects.filter(pk=i.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return i

    defect('Bar flap catch does not hold open', 'Ground floor bar, service end', 2,
           description='The flap drops when staff walk through. Could be dangerous during service.')
    defect('Scratch to bar front veneer panel', 'Ground floor bar, panel 3 from the left', 6, assignee=sub_user,
           due_date=today + timedelta(days=5))
    defect('Back bar shelf light flickering', 'Ground floor bar, back bar', 9, assignee=pm, status=Issue.STATUS_READY)

    # --- Leeds: O&M manual ---------------------------------------------------------------------
    services.setup_om(leeds, org)
    sections = {s.title: s for s in OMSection.objects.filter(project=leeds)}
    joinery = sections['Joinery']
    s = services.request_om(joinery, pm, due_date=today - timedelta(days=3),
                            note='Bar and back bar joinery, including the veneer care guide.')
    rev = SubmittalRevision.objects.create(submittal=s, number=0, submitted_by=sub_user,
                                           submitted_at=timezone.now() - timedelta(days=4))
    content = _pdf(demo_pdf, 'Joinery O&M information', 'Bar joinery: finishes, care and warranties',
                   'Joinery-OM.pdf')
    SubmittalFile.objects.create(revision=rev, file=content, original_name='Joinery-OM.pdf', size=content.size,
                                 uploaded_by=sub_user)
    apply_transition(s, 'submit', sub_user)
    rev.code, rev.reviewed_by, rev.reviewed_at = 'A', pm, timezone.now() - timedelta(days=2)
    rev.save()
    apply_transition(s, 'approve', pm)
    partitions = sections['Partitions and linings']
    if partitions.package_id:
        services.request_om(partitions, pm, due_date=today + timedelta(days=12))
    hs = sections['Health and safety file']
    doc = _pdf(demo_pdf, 'Health and safety file notes', 'Residual hazards and access for maintenance', 'HS-file.pdf')
    OMDocument.objects.create(section=hs, title='Residual hazards and maintenance access', file=doc,
                              original_name='HS-file.pdf', size=doc.size, uploaded_by=pm)
    OMSection.objects.filter(pk=sections['Mechanical services'].pk).update(not_needed=True)

    for kind, title, by, days in [('ELECTRICAL', '', 'Northern Electrical Services', 22),
                                  ('EMERGENCY_LIGHTING', 'Emergency lighting: ground floor bar', 'Northern Electrical Services', 21)]:
        f = _pdf(demo_pdf, title or 'Electrical installation certificate', 'Ground floor bar', f'{kind.lower()}.pdf')
        Certificate.objects.create(project=leeds, kind=kind, title=title, issued_by=by,
                                   issued_on=today - timedelta(days=days), file=f, original_name=f.name, size=f.size,
                                   uploaded_by=pm)

    comp = services.start_compilation(leeds, pm)
    comp.shared_with_client = True
    comp.save(update_fields=['shared_with_client'])
    pack = services.build_pack(leeds, pm, note='Handover of section 1, the ground floor bar.')
    if pack.status == pack.READY:
        services.issue_pack(pack, pm)

    # --- Salford: O&M set up, nothing asked for yet ---------------------------------------------
    salford = Project.objects.filter(name='Office CAT A, Salford Quays').first()
    if salford is not None:
        services.setup_om(salford, org)

    # --- Covent Garden: defects period ending soon -----------------------------------------------
    covent = Project.objects.filter(name='Restaurant, Covent Garden').first()
    if covent is not None:
        covent.manager = pm
        covent.save(update_fields=['manager'])
        services.set_completion(CompletionSection(
            project=covent, is_whole_works=True, name=CompletionSection.WHOLE_WORKS,
            pc_date=_months_ago(today, 6) + timedelta(days=24), defects_period_months=6), pm)
    return leeds


def _months_ago(day, months):
    from .rules import add_months
    return add_months(day, -months)

