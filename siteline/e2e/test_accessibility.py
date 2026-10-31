"""
Regression tests for the accessibility defects axe-core found in the role crawl (serious and
critical): text colours below WCAG AA contrast, an icon-only link with no name, selects with no
label and content outside any landmark. The crawl itself (Playwright with axe-core) is a dev tool
kept outside the repository; these pin the fixes.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import User
from accounts.views import create_organization
from directory.models import Subcontractor
from projects.models import Project, ProjectMember
from tenancy.context import org_context
from tenancy.models import Membership

CSS = Path(settings.BASE_DIR) / 'static' / 'css' / 'app.css'


def _lum(hex_colour):
    h = hex_colour.lstrip('#')
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def mix(a, b, share):
    """color-mix(in srgb, a share, b)"""
    a, b = a.lstrip('#'), b.lstrip('#')
    return '#' + ''.join(f'{round(int(a[i:i + 2], 16) * share + int(b[i:i + 2], 16) * (1 - share)):02x}'
                         for i in (0, 2, 4))


class ColourContrastTests(SimpleTestCase):
    """Every text colour token on the backgrounds it is used on, at 4.5:1 or better (WCAG 2.1 AA, normal text)."""

    def setUp(self):
        css = CSS.read_text()
        root = css[css.index(':root'):css.index('}', css.index(':root'))]
        self.t = {k: v.upper() for k, v in re.findall(r'--([\w-]+):\s*(#[0-9A-Fa-f]{6})', root)}
        self.css = css

    def test_secondary_text_on_every_background(self):
        for bg in ('paper', 'paper-2', 'card', 'bad-bg', 'warn-bg', 'muted-bg'):
            with self.subTest(bg=bg):
                self.assertGreaterEqual(contrast(self.t['ink-3'], self.t[bg]), 4.5)

    def test_status_badges(self):
        for tone in ('good', 'warn', 'bad', 'info'):
            with self.subTest(tone=tone):
                self.assertGreaterEqual(contrast(self.t[tone], self.t[f'{tone}-bg']), 4.5)

    def test_sidebar_secondary_text(self):
        for bg in (self.t['side'], '#23272C'):  # the sidebar and its raised panels (the Estimating/Projects switch)
            self.assertGreaterEqual(contrast(self.t['side-dim'], bg), 4.5)

    def test_accent_badge_for_any_brand_colour(self):
        rule = re.search(r'\.badge\.accent \{[^}]*color: color-mix\(in srgb, var\(--accent\) (\d+)%, black\)', self.css)
        share = int(rule.group(1)) / 100
        # Companies pick their own brand colour: light yellow is the hardest case
        for accent in ('#E8590C', '#F2C200', '#FFD43B', '#15AABF', '#FF6B6B', '#2B8A3E'):
            with self.subTest(accent=accent):
                self.assertGreaterEqual(contrast(mix(accent, '#000000', share), mix(accent, '#FFFFFF', 0.14)), 4.5)


class NamesAndLandmarksTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email='o@a11y.test', password='A11y-Pass-2026', full_name='Owen Owner')
        self.client_user = User.objects.create_user(email='c@a11y.test', password='A11y-Pass-2026', full_name='Cleo Client')
        self.org = create_organization('Access Fitout', self.owner)
        Membership.objects.create(organization=self.org, user=self.client_user, role=Membership.ROLE_EXTERNAL)
        with org_context(self.org):
            self.project = Project.objects.create(name='Bar job', stage=Project.STAGE_LIVE)
            ProjectMember.objects.create(project=self.project, user=self.client_user, role=ProjectMember.ROLE_CLIENT)
            Subcontractor.objects.create(company_name='Oak Joinery', email='oak@a11y.test')

    def get(self, user, name, *args):
        c = Client()
        c.force_login(user, backend='django.contrib.auth.backends.ModelBackend')
        return c.get(reverse(name, args=[self.org.slug, *args])).content.decode()

    def test_portal_home_link_has_a_name_when_its_text_is_hidden_on_phones(self):
        page = self.get(self.client_user, 'project_detail', self.project.pk)
        self.assertRegex(page, r'<a href="/access-fitout/"[^>]*aria-label="Home"')

    def test_staff_phone_top_bar_is_a_banner_landmark_and_the_sidebar_nav_is_named(self):
        page = self.get(self.owner, 'project_list')
        self.assertIn('<header class="topbar">', page)
        self.assertIn('<nav class="nav" aria-label="Main">', page)

    def test_filter_and_role_selects_are_labelled(self):
        page = self.get(self.owner, 'sub_list')
        self.assertIn('name="trade" data-autosubmit style="width:auto" aria-label="Trade"', page)
        self.assertIn('name="status" data-autosubmit style="width:auto" aria-label="Status"', page)
        page = self.get(self.owner, 'team')
        self.assertNotRegex(page, r'<select name="role" data-autosubmit(?![^>]*aria-label)')
