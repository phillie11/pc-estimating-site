"""
Shared set-up for the record-base tests: two companies, a live project each, staff and an
external client on Alpha's project. Copy this shape when testing a new record type.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from accounts.views import create_organization
from projects.models import Project, ProjectMember
from rfis.models import RFI
from tenancy.context import org_context
from tenancy.models import Membership

PW = 'Correct-Horse-9'


def make_user(email, name='Test User'):
    return User.objects.create_user(email=email, password=PW, full_name=name)


class RecordTestBase(TestCase):
    def setUp(self):
        self.alice = make_user('alice@a.test', 'Alice A')  # owner of Alpha
        self.bob = make_user('bob@b.test', 'Bob B')  # owner of Bravo
        self.carla = make_user('client@c.test', 'Carla Client')  # Alpha's client on pa
        self.org_a = create_organization('Alpha Fitout', self.alice)
        self.org_b = create_organization('Bravo Build', self.bob)
        Membership.objects.create(organization=self.org_a, user=self.carla, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org_a):
            self.pa = Project.objects.create(name='Alpha job', stage=Project.STAGE_LIVE)
            self.pa2 = Project.objects.create(name='Alpha second job', stage=Project.STAGE_LIVE)
            ProjectMember.objects.create(project=self.pa, user=self.carla, role=ProjectMember.ROLE_CLIENT)
        with org_context(self.org_b):
            self.pb = Project.objects.create(name='Bravo job', stage=Project.STAGE_LIVE)

    def rfi(self, project=None, org=None, actor=None, **kwargs):
        """Raise an RFI the way the views do: save(actor=...)."""
        project = project or self.pa
        org = org or self.org_a
        kwargs.setdefault('title', 'Which veneer?')
        kwargs.setdefault('question', '?')
        with org_context(org):
            r = RFI(project=project, **kwargs)
            r.save(actor=actor or self.alice)
            return r

    def login(self, user):
        self.client.force_login(user, backend='django.contrib.auth.backends.ModelBackend')

    def url(self, name, org, *args):
        return reverse(name, args=[org.slug, *args])
