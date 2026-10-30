"""
Demo data for document control, called by `seed_demo` inside the demo company's org_context:
an ISO-numbered drawing set on the Leeds job with superseded revisions, one transmittal
issued to the client and the joinery subcontractor (the client has acknowledged it), one
internal-only drawing issued to the subcontractor alone, and usual recipients for joinery.
"""
from django.core.files.base import ContentFile

from projects.models import Document, DocumentRevision

from . import services
from .models import DistributionRule, RegisterSettings, Transmittal, check_number


def seed(project, staff, client, sub_user, sub_company, pdf):
    """pdf(title, subtitle) -> bytes of a one-page PDF."""
    RegisterSettings.objects.create(project=project, project_code='CLL')
    drawings = [
        ('Ground floor general arrangement', 'CLL-NLD-ZZ-00-DR-A-1001', 'Architect', ['P01', 'P02', 'C01'], 'A1',
         Document.VIS_SHARED),
        ('Bar elevations', 'CLL-NLD-ZZ-00-DR-A-2101', 'Architect', ['P01', 'P02'], 'S2', Document.VIS_SHARED),
        ('Back-bar joinery details', 'CLL-LJL-ZZ-00-DR-X-5001', 'Subcontractor', ['P01'], 'S3', Document.VIS_INTERNAL),
        ('Kitchen extract layout', 'CLL-NLD-ZZ-01-DR-M-3001', 'Mechanical engineer', ['P01'], 'S0', Document.VIS_INTERNAL),
        ('Door schedule', 'CLL-NLD-ZZ-ZZ-SH-A-0001', 'Architect', ['P01'], 'S2', Document.VIS_SHARED),
        ('Lighting layout (consultant number)', 'E-LTG-101', 'Electrical engineer', ['T1'], '', Document.VIS_INTERNAL),
    ]
    current = {}
    for title, number, discipline, revs, suit, vis in drawings:
        doc = Document(project=project, title=title, category='DRAWING', visibility=vis, created_by=staff,
                       discipline=discipline, sheet_size='A1', scale='1:50')
        _, warnings = check_number(project, number)
        doc.set_number(number, warnings, accepted=False)
        doc.save()
        for i, code in enumerate(revs):
            last = i == len(revs) - 1
            body = pdf(title, f'{number} revision {code}')
            rev = DocumentRevision(revision=code, suitability=suit if last else 'S2',
                                   file=ContentFile(body, name=f'{number}_{code}.pdf'),
                                   original_name=f'{number}_{code}.pdf', size=len(body), uploaded_by=staff,
                                   page_count=1)
            doc.add_revision(rev)
        current[number] = doc.revisions.get(is_current=True)

    tr = services.quick_issue(staff, project, [current['CLL-NLD-ZZ-00-DR-A-1001'], current['CLL-NLD-ZZ-00-DR-A-2101']],
                              purpose=Transmittal.PURPOSE_CONSTRUCTION, title='GA and bar elevations for construction')
    if tr is not None:
        services.acknowledge(client, tr)
    # An internal drawing goes to the joinery subcontractor only: nobody else sees it
    tr2 = Transmittal(project=project, title='Back-bar joinery details for comment', purpose=Transmittal.PURPOSE_COMMENT)
    tr2.save(actor=staff)
    services.set_items(tr2, [current['CLL-LJL-ZZ-00-DR-X-5001']])
    services.set_recipients(tr2, [], [sub_company])
    services.issue(staff, tr2)
    DistributionRule.objects.create(project=project, discipline='Subcontractor', subcontractor=sub_company)
    return tr, tr2
