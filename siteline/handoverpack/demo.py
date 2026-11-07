"""
Demo data for the pricing workbook, called at the end of tenancy's seed_demo inside org_context: the
Kingsway bid's internal pricing workbook uploaded twice (revisions B then C, so the trade totals show
what changed), with its handover pack preview, clarifications, site rules and open items. Every name
in it is invented (handoverpack/sample.py). The bid is still tendering; winning it hands the pack over.
The York pub (won, not started) has its pack already handed to its project manager, so their Needs me
shows its open items.
"""
import copy

from django.core.files.uploadedfile import SimpleUploadedFile

from projects.models import Project

from . import sample, services


def _for(spec, project):
    spec = copy.deepcopy(spec)
    spec['header']['project'] = project.name
    spec['header']['client'] = project.client_name or spec['header']['client']
    for t in spec['trades'].values():
        for ln in t['lines']:
            ln['desc'] = (ln.get('desc') or '').replace('HL-', 'KW-')
    for ln in spec['joinery']:
        ln['desc'] = ln['desc'].replace('HL-', 'KW-')
    return spec


def seed(estimator):
    kings = Project.objects.get(name='Office CAT B, Kingsway')
    for spec in (sample.demo_revision_b(), sample.DEMO):
        data = sample.build(_for(spec, kings))
        f = SimpleUploadedFile(f'Kingsway pricing workbook rev {spec["header"]["revision"]}.xlsx', data)
        wb = services.upload(kings, estimator, f)
    pack = kings.handover_pack
    pack.site_rules = ('Working hours 08:00 to 18:00 Monday to Friday; noisy work before 10:00 or after 16:00 '
                       'needs the building manager\'s permit.\nDeliveries by the rear loading bay only, booked 48 '
                       'hours ahead.\nHot works permit from the landlord for any cutting or welding.')
    pack.save(update_fields=['site_rules', 'updated_at'])
    york = Project.objects.get(name='Pub refurbishment, York')
    f = SimpleUploadedFile('York pricing workbook rev C.xlsx', sample.build(_for(sample.DEMO, york)))
    services.upload(york, estimator, f)  # a won job: the pack reaches its PM at once (services.release)
    return wb
