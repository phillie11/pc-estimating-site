"""Follow-ups to the independent review: locked-day photos, uploads, image memory, stored lock, dates, CSV."""
import io
import os
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from core.models import AuditEntry
from tenancy.context import org_context

from .. import photos
from ..models import DelayLine, DiaryEntry, DiarySettings, SitePhoto
from .test_diary import DiaryTestBase, jpeg


class ReviewFollowUps(DiaryTestBase):
    def test_locked_day_photo_can_still_be_shared_and_it_is_audited(self):
        old = self.old_entry()
        p = self.photo(entry=old)
        self.login(self.sam)
        r = self.client.get(self.url('diary_photo', self.pa.pk, p.pk))
        self.assertNotContains(r, 'name="caption"')
        self.client.post(self.url('diary_photo', self.pa.pk, p.pk), {'shared_with_client': 'on', 'caption': 'x'})
        with org_context(self.org_a):
            p.refresh_from_db()
            self.assertTrue(p.shared_with_client)
            self.assertEqual(p.caption, 'Bar back')
            self.assertTrue(AuditEntry.objects.filter(record_idx__object_id=old.pk, action='photo shared').exists())

    def test_attached_photo_keeps_the_diary_date(self):
        e = self.entry()
        p = self.photo(entry=e)
        self.login(self.pat)
        self.client.post(self.url('diary_photo', self.pa.pk, p.pk), {
            'caption': 'x', 'area': '', 'taken_on': (self.today - timedelta(days=3)).isoformat()})
        with org_context(self.org_a):
            p.refresh_from_db()
        self.assertEqual(p.taken_on, e.entry_date)
        self.assertEqual(p.caption, 'x')

    def test_failed_upload_leaves_no_files(self):
        before = sum(len(f) for _, _, f in os.walk(settings.MEDIA_ROOT))
        buf = io.BytesIO()
        Image.new('RGB', (900, 700), 'red').save(buf, 'JPEG')
        bad = SimpleUploadedFile('bad.jpg', buf.getvalue()[:2000], content_type='image/jpeg')
        self.login(self.pat)
        r = self.client.post(self.url('diary_photo_upload', self.pa.pk), {
            'library': [jpeg('good.jpg'), bad], 'taken_on': self.today.isoformat()})
        self.assertContains(r, 'could not be read')
        after = sum(len(f) for _, _, f in os.walk(settings.MEDIA_ROOT))
        self.assertEqual(before, after)
        with org_context(self.org_a):
            self.assertFalse(SitePhoto.objects.exists())

    def test_big_png_refused_from_its_header(self):
        buf = io.BytesIO()
        Image.new('L', (8000, 7400)).save(buf, 'PNG')
        f = SimpleUploadedFile('huge.png', buf.getvalue(), content_type='image/png')
        with mock.patch.object(photos, 'derivatives') as decode, self.assertRaisesMessage(ValidationError, 'too large'):
            photos.validate_photo(f)
        decode.assert_not_called()  # refused from the header, never decoded

    def test_png_is_reduced_before_it_is_converted_or_turned(self):
        buf = io.BytesIO()
        Image.new('LA', (6000, 5000)).save(buf, 'PNG')
        seen = []
        real_rgb, real_upright = photos._to_rgb, photos._upright

        def rgb(img):
            seen.append(('rgb', img.size))
            return real_rgb(img)

        def upright(img, orientation):
            seen.append(('upright', img.size))
            return real_upright(img, orientation)
        with mock.patch.object(photos, '_to_rgb', rgb), mock.patch.object(photos, '_upright', upright):
            display, thumb, w, h = photos.derivatives(SimpleUploadedFile('a.png', buf.getvalue()))
        self.assertEqual((w, h), (6000, 5000))
        self.assertEqual([step for step, _ in seen], ['upright', 'rgb'])
        self.assertTrue(all(max(size) <= photos.DISPLAY_SIZE for _, size in seen), seen)

    def test_original_dimensions_follow_rotation(self):
        img = Image.new('RGB', (4000, 3000))
        exif = img.getexif()
        exif[0x0112] = 6
        buf = io.BytesIO()
        img.save(buf, 'JPEG', exif=exif)
        display, thumb, w, h = photos.derivatives(SimpleUploadedFile('r.jpg', buf.getvalue()))
        self.assertEqual((w, h), (3000, 4000))
        self.assertEqual(Image.open(io.BytesIO(display)).size, (1200, 1600))

    def test_lock_is_stored_so_a_settings_change_never_reopens_a_day(self):
        old = self.old_entry(days_ago=3)
        with org_context(self.org_a):
            first = DiaryEntry.objects.get(pk=old.pk)
            DiarySettings.objects.create(saturday_counts=True)
            again = DiaryEntry.objects.get(pk=old.pk)
        self.assertIsNotNone(first.locks_at)
        self.assertEqual((again.locks_at, again.locked), (first.locks_at, first.locked))

    def test_unlock_moves_the_stored_lock(self):
        old = self.old_entry()
        self.login(self.pat)
        self.client.post(self.day_url('diary_lock', old.entry_date), {'action': 'unlock', 'reason': 'Correction'})
        with org_context(self.org_a):
            e = DiaryEntry.objects.get(pk=old.pk)
        self.assertGreater(e.locks_at, old.locks_at)
        self.assertFalse(e.locked)

    def test_no_entries_before_the_start_on_site_or_far_back(self):
        self.login(self.pat)
        before_start = self.today - timedelta(days=70)  # after the job was set up, before the start on site
        self.assertEqual(self.client.get(self.day_url('diary_day', before_start)).status_code, 200)
        r = self.client.post(self.url('diary_line_add', self.pa.pk, before_start.isoformat(), 'labour'),
                             {'name': 'x', 'headcount': 1})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.get(self.day_url('diary_day', self.today - timedelta(days=120))).status_code, 404)
        self.assertEqual(self.client.get(self.url('diary_day', self.pa.pk, '1999-12-31')).status_code, 404)
        self.assertEqual(self.client.get(self.url('diary_day', self.pa.pk, '2026-13-45')).status_code, 404)
        with org_context(self.org_a):
            self.assertFalse(DiaryEntry.objects.exists())

    def test_soft_deleted_day_is_a_409_not_a_500(self):
        e = self.entry()
        with org_context(self.org_a):
            e.soft_delete(self.adam)
        self.login(self.sam)
        self.client.raise_request_exception = False
        r = self.client.post(self.url('diary_line_add', self.pa.pk, self.today.isoformat(), 'labour'),
                             {'name': 'x', 'headcount': 1})
        self.assertContains(r, 'was deleted', status_code=409)

    def test_csv_strips_spaces_before_the_formula_check(self):
        with org_context(self.org_a):
            DelayLine.objects.create(entry=self.entry(), cause='OTHER', hours_lost=1, description='   =1+1',
                                     trades_affected='\t@SUM(A1)', created_by=self.pat)
        self.login(self.pat)
        body = self.client.get(self.url('diary_delays', self.pa.pk) + '?export=csv').content.decode()
        self.assertIn("'   =1+1", body)
        self.assertIn("'\t@SUM(A1)", body)
