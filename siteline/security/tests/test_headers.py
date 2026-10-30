"""Security headers, self-hosted fonts and `check --deploy`."""
import os
import re
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from security.middleware import build_csp

from .base import SecurityTestBase

SITELINE = Path(settings.BASE_DIR)


class HeaderTests(SecurityTestBase):
    def test_headers_on_public_and_app_pages(self):
        self.login(self.owner)
        for url in (reverse('login'), self.url('dashboard')):
            if url == reverse('login'):
                self.client.logout()
            r = self.client.get(url)
            csp = r['Content-Security-Policy']
            for part in ("default-src 'self'", "script-src 'self'", "font-src 'self'", "object-src 'none'",
                         "frame-ancestors 'none'", "base-uri 'self'", "form-action 'self'"):
                self.assertIn(part, csp, url)
            self.assertNotIn('unsafe-eval', csp)
            self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
            self.assertNotIn('googleapis', csp)
            self.assertIn('camera=()', r['Permissions-Policy'])
            self.assertEqual(r['Referrer-Policy'], 'same-origin')
            self.assertEqual(r['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(r['X-Frame-Options'], 'DENY')
            self.assertEqual(r['Cross-Origin-Opener-Policy'], 'same-origin')
            self.login(self.owner)

    def test_templates_have_no_inline_scripts_or_handlers(self):
        """What the CSP's script-src 'self' relies on."""
        offenders = []
        for path in SITELINE.rglob('*.html'):
            if 'staticfiles' in path.parts or 'node_modules' in path.parts:
                continue
            text = path.read_text()
            for tag in re.findall(r'<script\b[^>]*>', text):
                if 'src=' not in tag and 'application/json' not in tag:
                    offenders.append(f'{path}: {tag}')
            if re.search(r'\son[a-z]+\s*=\s*["\']', text):
                offenders.append(f'{path}: inline event handler')
            if re.search(r'(src|href)=["\']https?://(?!www\.w3\.org)', text):
                offenders.append(f'{path}: loads from another site')
        self.assertEqual(offenders, [])

    @override_settings(CSP_REPORT_ONLY=True)
    def test_report_only_switch(self):
        r = self.client.get(reverse('login'))
        self.assertIn('Content-Security-Policy-Report-Only', r)
        self.assertNotIn('Content-Security-Policy', r)

    @override_settings(STORAGE_BUCKET='siteline-files',
                       STORAGES={'default': {'OPTIONS': {'endpoint_url': 'https://s3.eu-west-2.amazonaws.com'}},
                                 'staticfiles': settings.STORAGES['staticfiles']})
    def test_cloud_storage_images_allowed(self):
        img = re.search(r'img-src ([^;]+)', build_csp()).group(1)
        self.assertIn('https://s3.eu-west-2.amazonaws.com', img)
        self.assertIn('https://*.s3.eu-west-2.amazonaws.com', img)


class FontTests(SecurityTestBase):
    def test_pages_load_fonts_from_here_not_google(self):
        r = self.client.get(reverse('login'))
        self.assertContains(r, '/static/fonts/fonts.css')
        self.assertNotContains(r, 'fonts.googleapis.com')
        self.assertNotContains(r, 'fonts.gstatic.com')

    def test_font_files_exist_and_are_woff2(self):
        css = Path(finders.find('fonts/fonts.css')).read_text()
        self.assertIn("font-family: 'Archivo'", css)
        self.assertIn("font-family: 'IBM Plex Mono'", css)
        files = re.findall(r'url\(([^)]+)\)', css)
        self.assertEqual(len(files), 6)
        for name in files:
            path = finders.find(f'fonts/{name}')
            self.assertIsNotNone(path, name)
            self.assertEqual(Path(path).read_bytes()[:4], b'wOF2', name)
        self.assertIsNotNone(finders.find('fonts/Archivo-OFL.txt'))
        self.assertIsNotNone(finders.find('fonts/IBMPlexMono-OFL.txt'))

    @override_settings(WHITENOISE_USE_FINDERS=True, WHITENOISE_AUTOREFRESH=True)
    def test_fonts_are_served(self):
        r = self.client.get('/static/fonts/archivo-latin.woff2')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'font/woff2')
        self.assertEqual(self.client.get('/static/fonts/fonts.css').status_code, 200)


class DeployCheckTests(SimpleTestCase):
    """Production settings: HSTS on by default, and check --deploy down to the two documented choices."""

    def run_python(self, code, **env):
        environ = {k: v for k, v in os.environ.items() if k not in ('DEBUG', 'SECURE_HSTS_SECONDS')}
        environ.update({'SECRET_KEY': 'x' * 30 + 'Ab3$' * 10, 'ALLOWED_HOSTS': 'siteline.example',
                        'DJANGO_SETTINGS_MODULE': 'config.settings', **env})
        return subprocess.run([sys.executable, '-c', code], cwd=SITELINE, env=environ,
                              capture_output=True, text=True, timeout=120)

    def test_hsts_defaults_to_a_year_and_can_be_overridden(self):
        code = 'import django; django.setup(); from django.conf import settings as s; print(s.SECURE_HSTS_SECONDS)'
        self.assertEqual(self.run_python(code).stdout.strip(), '31536000')
        self.assertEqual(self.run_python(code, SECURE_HSTS_SECONDS='600').stdout.strip(), '600')
        self.assertEqual(self.run_python(code, DEBUG='1').stdout.strip(), '0')

    def test_check_deploy_only_reports_the_documented_choices(self):
        code = ('import sys, django; django.setup(); from django.core.management import call_command\n'
                'call_command("check", deploy=True)')
        result = self.run_python(code)
        output = result.stdout + result.stderr
        ids = set(re.findall(r'\(([a-z_]+\.[EW]\d+)\)', output))
        self.assertEqual(ids, {'security.W005', 'security.W021'}, output)
