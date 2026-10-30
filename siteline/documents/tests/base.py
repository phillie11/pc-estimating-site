"""
Shared set-up for document-control tests: two companies; on Alpha's live job a staff
member, a client, a consultant, two appointed subcontractors (different companies), a
tendering subcontractor, and an external in the company who isn't on the project.
"""
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from accounts.views import create_organization
from directory.models import Subcontractor
from projects.models import Document, DocumentRevision, Project, ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership

PW = 'Correct-Horse-9'


def make_user(email, name):
    return User.objects.create_user(email=email, password=PW, full_name=name)


def pdf_bytes(pages=1):
    """A minimal valid PDF with `pages` pages, for page-count tests."""
    objs = ['<< /Type /Catalog /Pages 2 0 R >>',
            '<< /Type /Pages /Kids [' + ' '.join(f'{3 + i} 0 R' for i in range(pages)) + f'] /Count {pages} >>']
    objs += ['<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>'] * pages
    out, offsets = b'%PDF-1.4\n', []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n{body}\nendobj\n'.encode()
    xref = len(out)
    out += f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n'.encode()
    out += b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    out += f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return out


def upload(name, content=None):
    return SimpleUploadedFile(name, content if content is not None else pdf_bytes(), content_type='application/pdf')


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DocBase(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=self.media)
        media.enable()
        self.addCleanup(media.disable)

        self.alice = make_user('alice@a.test', 'Alice Owner')
        self.admin = make_user('admin@a.test', 'Adam Admin')
        self.staff = make_user('staff@a.test', 'Sam Staff')
        self.cli = make_user('client@c.test', 'Carla Client')
        self.con = make_user('consult@c.test', 'Colin Consultant')
        self.sub1 = make_user('sub1@s.test', 'Sid Joiner')
        self.sub1b = make_user('sub1b@s.test', 'Sue Joiner')  # same company as sub1
        self.sub2 = make_user('sub2@s.test', 'Stan Sparks')
        self.bidder = make_user('bid@s.test', 'Bea Bidder')
        self.outsider = make_user('out@x.test', 'Olly Outsider')
        self.bob = make_user('bob@b.test', 'Bob Bravo')
        self.org_a = create_organization('Alpha Fitout', self.alice)
        self.org_b = create_organization('Bravo Build', self.bob)
        Membership.objects.create(organization=self.org_a, user=self.admin, role=Membership.ROLE_ADMIN)
        Membership.objects.create(organization=self.org_a, user=self.staff, role=Membership.ROLE_MEMBER)
        for u in (self.cli, self.con, self.sub1, self.sub1b, self.sub2, self.bidder, self.outsider):
            Membership.objects.create(organization=self.org_a, user=u, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            self.pa = Project.objects.create(name='Alpha job', stage=Project.STAGE_LIVE)
            self.joiners = Subcontractor.objects.create(company_name='Oak Joinery')
            self.sparks = Subcontractor.objects.create(company_name='Bright Sparks')
            ProjectMember.objects.create(project=self.pa, user=self.cli, role=ProjectMember.ROLE_CLIENT)
            ProjectMember.objects.create(project=self.pa, user=self.con, role=ProjectMember.ROLE_CONSULTANT)
            ProjectMember.objects.create(project=self.pa, user=self.sub1, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.joiners)
            ProjectMember.objects.create(project=self.pa, user=self.sub1b, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.joiners)
            ProjectMember.objects.create(project=self.pa, user=self.sub2, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         subcontractor=self.sparks)
            ProjectMember.objects.create(project=self.pa, user=self.bidder, role=ProjectMember.ROLE_BIDDER)
        with org_context(self.org_b):
            self.pb = Project.objects.create(name='Bravo job', stage=Project.STAGE_LIVE)

    # --- helpers --------------------------------------------------------------
    def login(self, user):
        self.client.force_login(user, backend='django.contrib.auth.backends.ModelBackend')

    def url(self, name, *args, org=None):
        org = org or self.org_a
        return reverse(name, args=[org.slug, *args])

    def purl(self, name, *args):
        return self.url(name, self.pa.pk, *args)

    def doc(self, number='SLN-ABC-ZZ-02-DR-A-1001', title='GA plan', revisions=('P01',), visibility=Document.VIS_INTERNAL,
            project=None, org=None, **kwargs):
        """A document with revisions added the way the views add them; returns (doc, {code: revision})."""
        org, project = org or self.org_a, project or self.pa
        with org_context(org):
            from documents.models import check_number
            d = Document(project=project, title=title, visibility=visibility, created_by=self.alice, **kwargs)
            d.set_number(number, check_number(project, number)[1])
            d.save()
            revs = {}
            for code in revisions:
                revs[code] = d.add_revision(DocumentRevision(revision=code, file=upload(f'{code}.pdf'),
                                                             original_name=f'{code}.pdf', size=10,
                                                             uploaded_by=self.alice))
        return d, revs

    def fresh(self, obj, org=None):
        with org_context(org or self.org_a):
            return type(obj).all_objects.get(pk=obj.pk)
