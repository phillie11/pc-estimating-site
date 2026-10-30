"""Set-up shared by the security tests: two companies and one person in every role."""
import time

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.views import create_organization
from projects.models import Project, ProjectMember
from security import totp
from security.models import RecoveryCode, TwoFactorDevice
from tenancy.context import org_context
from tenancy.models import Membership

PW = 'Correct-Horse-9'


def make_user(email, name='Test User'):
    return User.objects.create_user(email=email, password=PW, full_name=name)


def code_for(device, offset=0):
    """A valid code for the device, `offset` 30-second steps from now (one either side is accepted)."""
    return totp.hotp(totp._key(device.secret), totp.time_step() + offset)


class SecurityTestBase(TestCase):
    def setUp(self):
        self.owner = make_user('owner@a.test', 'Olive Owner')
        self.admin = make_user('admin@a.test', 'Adam Admin')
        self.staff = make_user('staff@a.test', 'Sam Staff')
        self.client_user = make_user('client@c.test', 'Carla Client')
        self.sub_user = make_user('sub@s.test', 'Sid Sub')
        self.stranger = make_user('nobody@x.test', 'Nora Nobody')
        self.other_owner = make_user('bob@b.test', 'Bob Bravo')
        self.org = create_organization('Alpha Fitout', self.owner)
        self.org_b = create_organization('Bravo Build', self.other_owner)
        self.m_admin = Membership.objects.create(organization=self.org, user=self.admin, role=Membership.ROLE_ADMIN)
        self.m_staff = Membership.objects.create(organization=self.org, user=self.staff, role=Membership.ROLE_MEMBER)
        self.m_client = Membership.objects.create(organization=self.org, user=self.client_user,
                                                  role=Membership.ROLE_EXTERNAL)
        self.m_sub = Membership.objects.create(organization=self.org, user=self.sub_user, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org):
            self.project = Project.objects.create(name='Alpha job', stage=Project.STAGE_LIVE)
            ProjectMember.objects.create(project=self.project, user=self.client_user, role=ProjectMember.ROLE_CLIENT)
            ProjectMember.objects.create(project=self.project, user=self.sub_user, role=ProjectMember.ROLE_SUBCONTRACTOR)

    # -- helpers ---------------------------------------------------------------

    def login(self, user):
        self.client.force_login(user, backend='django.contrib.auth.backends.ModelBackend')

    def url(self, name, *args, org=None):
        return reverse(name, args=[(org or self.org).slug, *args])

    def enrol(self, user):
        """Give `user` a confirmed authenticator and recovery codes; returns (device, codes)."""
        device = TwoFactorDevice.objects.create(user=user, secret=totp.new_secret(), confirmed_at=timezone.now())
        return device, RecoveryCode.generate_for(user)

    def password_step(self, user, password=PW, **extra):
        return self.client.post(reverse('login'), {'username': user.email, 'password': password, **extra})

    def make_stale(self, seconds=601):
        session = self.client.session
        session['sl_auth_at'] = int(time.time()) - seconds
        session.save()
