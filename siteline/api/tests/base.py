"""
A job with something in every register the API serves, made through the owning modules, on top of
cvr/tests/base.py's money (applications, payments, the cost ledger, a client variation). Bravo, the
other company, gets one of everything too, so every endpoint can be tried from both sides.

As in changes/tests/base.py: alice owner, adam admin, pat the job's PM, eve estimator, carla client,
connie consultant, sid subcontractor, bea tendering sub, nora external, bob owner of another company.
"""
from datetime import timedelta
from decimal import Decimal as D

from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone

from api import auth
from api.models import SCOPE_CHOICES
from changes.models import ChangeEvent
from contracts.models import MainContract
from payments.models import Application
from variations.models import Variation
from costs import services as cost_services
from costs.access import settings_for as cost_settings_for
from costs.models import CostCode
from cvr.tests.base import CVRTestBase
from directory.models import Package, Subcontractor
from projects.models import Document, DocumentRevision, Issue, Project
from rfis.models import RFI
from safety.models import Incident
from subpayments import services as sub_services
from subpayments.models import subcontract_for
from tenancy.context import org_context

ALL_SCOPES = [code for code, _ in SCOPE_CHOICES]
PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'


def make_records(org, project, actor, subco=None, package=None):
    """One of each: RFI, change event, drawing with a revision, snag, defect, incident, sub payment."""
    out = {}
    with org_context(org):
        rfi = RFI(project=project, title='Ceiling void depth', question='What depth is the void at grid C?',
                  due_date=timezone.localdate() - timedelta(days=3))
        rfi.save(actor=actor)
        ce = ChangeEvent(project=project, title='Extra sockets', description='Client asked for six more.')
        ce.save(actor=actor)
        doc = Document.objects.create(project=project, title='Ground floor GA', reference='A-100', created_by=actor)
        content = ContentFile(PDF, name='A-100-P01.pdf')
        rev = doc.add_revision(DocumentRevision(revision='P01', file=content, original_name='A-100-P01.pdf',
                                                size=content.size, uploaded_by=actor))
        snag = Issue.objects.create(project=project, title='Scuffed skirting', issue_type='SNAG', created_by=actor)
        defect = Issue.objects.create(project=project, title='Door closer leaking', issue_type='DEFECT',
                                      due_date=timezone.localdate() - timedelta(days=2), created_by=actor)
        inc = Incident(project=project, title='Slip by the goods lift', description='Wet floor, nobody hurt.',
                       location='Goods lift', kind=Incident.KIND_NEAR_MISS)
        inc.save(actor=actor)
        if package is None:
            subco = subco or Subcontractor.objects.create(company_name='Other Joinery')
            package = Package.objects.create(project=project, subcontractor=subco, status=Package.APPOINTED,
                                             quote_value=D('40000'))
            cost_settings_for(org)
            cost_services.commit_package(package, CostCode.objects.get(code='220'), actor)
        sc = subcontract_for(package, actor)
        valuation = timezone.localdate() - timedelta(days=5)
        sa = sub_services.receive_application(sc, actor, valuation, D('10000'), check_date=False)
        pay = sub_services.record_payment(sa, actor, D('5000'), timezone.localdate())
        out.update(rfi=rfi, ce=ce, doc=doc, rev=rev if isinstance(rev, DocumentRevision) else doc.revisions.first(),
                   snag=snag, defect=defect, incident=inc, sub_app=sa, sub_payment=pay)
    return out


@override_settings(RATELIMIT_ENABLED=True)
class ApiTestBase(CVRTestBase):
    def setUp(self):
        super().setUp()
        self.money = self.build_money()
        self.a = make_records(self.org_a, self.pa, self.pat, package=self.money['pkg'])
        with org_context(self.org_b):
            self.pb.value = D('50000.00')
            self.pb.save()
        self.b = make_records(self.org_b, self.pb, self.bob)
        with org_context(self.org_b):
            contract = MainContract(project=self.pb, form='JCT_IC', retention_percent=D('3'), valuation_day=25)
            contract.apply_preset(only_blank=True, creating=True)
            contract.save()
            vo = Variation(project=self.pb, title='Bravo extra', description='More.', value=D('900'))
            vo.save(actor=self.bob)
            ap = Application(project=self.pb, valuation_date=timezone.localdate() - timedelta(days=10),
                             work_done=D('10000'), title='Bravo valuation 1')
            ap.recalculate(contract)
            ap.save(actor=self.bob)
            self.b.update(vo=vo, ap1=ap)
        self.token_a, self.text_a = self.make_token(self.org_a, self.alice)
        self.token_b, self.text_b = self.make_token(self.org_b, self.bob)

    def make_token(self, org, user, scopes=None, name='Power BI', expires_at=None):
        with org_context(org):
            return auth.create_token(org, user, name, ALL_SCOPES if scopes is None else scopes, expires_at)

    def api(self, path, text=None, method='get', **params):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {text}'} if text else {}
        return getattr(self.client, method)(path, params, **headers)

    def archive(self, project, org):
        with org_context(org):
            Project.objects.filter(pk=project.pk).update(archived_at=timezone.now())
