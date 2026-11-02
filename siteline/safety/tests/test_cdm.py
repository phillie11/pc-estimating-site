"""
CDM register: duty holders with sensible defaults, the notifiable check, the construction phase
plan uploaded and versioned (and served only to staff on the job), and its Needs me items.
"""
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from tenancy.context import org_context

from ..actions import items
from ..models import CDMProfile, DutyHolder, PhasePlan
from .base import SafetyTestBase


def pdf(name='cpp.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4\n%demo\n', content_type='application/pdf')


class DutyHolderTests(SafetyTestBase):
    def test_add_with_defaults_change_and_remove(self):
        self.login(self.sam)
        with org_context(self.org_a):
            self.pa.client_name = 'Neon Leisure'
            self.pa.save()
        r = self.client.get(self.u('duty_holder_add', self.pa.pk) + '?role=CLIENT')
        self.assertContains(r, 'value="Neon Leisure"')
        r = self.client.get(self.u('duty_holder_add', self.pa.pk) + '?role=PC')
        self.assertContains(r, 'value="Alpha Fitout"')  # the principal contractor is usually us
        self.client.post(self.u('duty_holder_add', self.pa.pk), {'role': 'PD', 'company': 'Arc Design'})
        with org_context(self.org_a):
            h = DutyHolder.objects.get()
        page = self.client.get(self.u('cdm_register', self.pa.pk))
        self.assertContains(page, 'Arc Design')
        self.assertContains(page, 'Add the client')
        self.assertContains(page, 'Add the principal contractor')
        self.assertNotContains(page, 'Add the principal designer')
        self.client.post(self.u('duty_holder_edit', self.pa.pk, h.pk), {'role': 'PD', 'company': 'Arc Design Ltd'})
        self.client.post(self.u('duty_holder_edit', self.pa.pk, h.pk), {'action': 'delete'})
        with org_context(self.org_a):
            self.assertFalse(DutyHolder.objects.exists())

    def test_another_jobs_duty_holder_is_404(self):
        with org_context(self.org_b):
            other = DutyHolder.objects.create(project=self.pb, role='CLIENT', company='Bravo client')
        self.login(self.pat)
        self.assertEqual(self.client.get(self.u('duty_holder_edit', self.pa.pk, other.pk)).status_code, 404)


class NotificationTests(SafetyTestBase):
    def test_suggestion_and_f10(self):
        self.login(self.pat)
        r = self.client.post(self.u('cdm_edit', self.pa.pk), {'working_days': '40', 'peak_workers': '25'})
        self.assertEqual(r.status_code, 302)
        self.assertContains(self.client.get(self.u('cdm_register', self.pa.pk)), 'looks <strong>notifiable</strong>')
        r = self.client.post(self.u('cdm_edit', self.pa.pk), {'notifiable': 'NO', 'f10_reference': 'F10/1'})
        self.assertContains(r, 'An F10 has been recorded')
        self.client.post(self.u('cdm_edit', self.pa.pk), {'notifiable': 'YES'})
        with org_context(self.org_a):
            cdm = CDMProfile.objects.get(project=self.pa)
            self.assertEqual((cdm.notifiable, cdm.updated_by), ('YES', self.pat))
            got = [x for x in items(self.request_for(self.pat)) if x.key == f'safety:f10:{self.pa.pk}']
            self.assertEqual(len(got), 1)
        self.client.post(self.u('cdm_edit', self.pa.pk), {'notifiable': 'YES', 'f10_reference': 'F10/1',
                                                          'f10_submitted_on': timezone.localdate().isoformat()})
        with org_context(self.org_a):
            self.assertFalse([x for x in items(self.request_for(self.pat)) if x.key.startswith('safety:f10')])


class PhasePlanTests(SafetyTestBase):
    def test_versions_and_serving(self):
        self.login(self.sam)
        self.client.post(self.u('phase_plan_upload', self.pa.pk), {'file': pdf('cpp-v1.pdf')})
        r = self.client.post(self.u('phase_plan_upload', self.pa.pk), {'file': pdf('cpp-v2.pdf'), 'note': 'Level 2'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            v1, v2 = PhasePlan.objects.order_by('version')
        self.assertEqual((v1.version, v2.version, v2.note), (1, 2, 'Level 2'))
        page = self.client.get(self.u('cdm_register', self.pa.pk))
        self.assertContains(page, 'Superseded')
        self.assertContains(page, 'Current')
        self.assertEqual(self.client.get(v1.get_absolute_url()).status_code, 200)
        for user, code in ((self.stan, 403), (self.carla, 403), (self.sid, 403), (self.nora, 404), (self.bob, 404)):
            with self.subTest(user=user.email):
                self.assertEqual(self.status_for(user, v1.get_absolute_url()), code)

    def test_blocked_file_types_refused(self):
        self.login(self.sam)
        r = self.client.post(self.u('phase_plan_upload', self.pa.pk),
                             {'file': SimpleUploadedFile('plan.html', b'<script>', content_type='text/html')})
        self.assertContains(r, '.html files are not allowed')

    def test_missing_plan_on_the_pms_needs_me_until_uploaded_or_dated(self):
        with org_context(self.org_a):
            got = [x for x in items(self.request_for(self.pat)) if x.key == f'safety:cpp:{self.pa.pk}']
            self.assertEqual([x.due for x in got], [self.pa.start_date])
            self.assertEqual(items(self.request_for(self.sam)), [])  # only the job's manager
            cdm = CDMProfile.for_project(self.pa)
            cdm.cpp_in_place_on = timezone.localdate()
            cdm.save()
            self.assertFalse([x for x in items(self.request_for(self.pat)) if x.key.startswith('safety:cpp')])

    def test_not_chased_too_early(self):
        with org_context(self.org_a):
            self.pa.start_date = timezone.localdate() + timedelta(days=30)
            self.pa.save()
            self.assertFalse([x for x in items(self.request_for(self.pat)) if x.key.startswith('safety:cpp')])

    def test_home_lists_what_is_missing(self):
        self.login(self.sam)
        r = self.client.get(self.u('safety_home', self.pa.pk))
        for words in ('No client recorded', 'No principal designer recorded', 'No principal contractor recorded',
                      'No construction phase plan uploaded', 'notifiable to HSE'):
            self.assertContains(r, words)
