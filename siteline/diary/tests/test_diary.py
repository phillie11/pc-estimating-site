"""
Site diary: numbering and index, the lock window and unlock audit through the views, line adds
audited on the entry, photo validation and thumbnails, who sees shared photos (per role), the
missing-entry "Needs me" rule (weekends, Saturday setting), the registers, PDF and CSV, site
mode markup, cross-company isolation and failing closed with no organisation.
"""
import io
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image

import actions
from changes.tests.base import CommercialTestBase, make_user
from core.models import AuditEntry, RecordIndex
from directory.models import Package
from projects.models import ProjectMember
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Membership

from ..actions import items
from ..models import DelayLine, DiaryEntry, DiarySettings, LabourLine, SitePhoto
from ..photos import attach

MEDIA = tempfile.mkdtemp(prefix='diary-tests-')


def jpeg(name='site.jpg', size=(800, 600), colour='#888'):
    buf = io.BytesIO()
    Image.new('RGB', size, colour).save(buf, 'JPEG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


@override_settings(MEDIA_ROOT=MEDIA)
class DiaryTestBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        # sam: site manager on the team (not PM); stan: projects staff not on this job
        self.sam, self.stan = make_user('sam@a.test', 'Sam Site'), make_user('stan@a.test', 'Stan Staff')
        Membership.objects.create(organization=self.org_a, user=self.sam, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        Membership.objects.create(organization=self.org_a, user=self.stan, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.sam, role=ProjectMember.ROLE_SITE)

    def entry(self, day=None, project=None, org=None, actor=None, written=None):
        day = day or self.today
        with org_context(org or self.org_a):
            e = DiaryEntry(project=project or self.pa, entry_date=day)
            e.save(actor=actor or self.pat)
            if written:  # backdate when it was written (set-up only)
                DiaryEntry.objects.filter(pk=e.pk).update(created_at=timezone.make_aware(
                    datetime.combine(written, datetime.min.time().replace(hour=17))))
                e = DiaryEntry.objects.get(pk=e.pk)
            return e

    def old_entry(self, days_ago=5):
        day = self.today - timedelta(days=days_ago)
        return self.entry(day, written=day)

    def photo(self, shared=False, entry=None, project=None, org=None):
        with org_context(org or self.org_a):
            p = SitePhoto(project=project or self.pa, entry=entry, caption='Bar back', area='Bar',
                          uploaded_by=self.pat, shared_with_client=shared)
            attach(p, jpeg())
            p.save()
            return p

    def day_url(self, name, day=None, *extra):
        return self.url(name, self.pa.pk, (day or self.today).isoformat(), *extra)


class RecordTests(DiaryTestBase):
    def test_numbered_internal_one_per_day_and_indexed(self):
        a = self.entry(self.today - timedelta(days=1))
        b = self.entry()
        other = self.entry(project=self.pb, org=self.org_b, actor=self.bob)
        self.assertEqual((a.ref, b.ref, other.ref), ('DY-001', 'DY-002', 'DY-001'))
        self.assertEqual(b.visibility, 'INTERNAL')
        self.assertTrue(b.title.startswith('Site diary '))
        with org_context(self.org_a):
            self.assertEqual(RecordIndex.objects.filter(record_type='diary.diaryentry').count(), 2)
        from django.db import IntegrityError, transaction
        with org_context(self.org_a), self.assertRaises(IntegrityError), transaction.atomic():
            DiaryEntry(project=self.pa, entry_date=self.today).save(actor=self.pat)

    def test_externals_never_see_entries_through_the_index(self):
        self.entry()
        from core.access import visible_index
        for u in (self.carla, self.sid):
            with org_context(self.org_a):
                self.assertFalse(visible_index(self.request_for(u)).filter(record_type='diary.diaryentry').exists())


class LockTests(DiaryTestBase):
    def labour(self, day):
        return self.client.post(self.url('diary_line_add', self.pa.pk, day.isoformat(), 'labour'),
                                {'name': 'Rapid Labour', 'headcount': '3'})

    def test_add_today_creates_entry_and_audits(self):
        self.login(self.sam)
        r = self.labour(self.today)
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            e = DiaryEntry.objects.get(project=self.pa, entry_date=self.today)
            self.assertEqual(e.labour.get().headcount, 3)
            actions_ = list(AuditEntry.objects.filter(record_idx__object_id=e.pk,
                                                      record_idx__record_type='diary.diaryentry')
                            .values_list('action', flat=True))
        self.assertEqual(actions_, ['created', 'labour added'])

    def test_yesterday_open_older_locked(self):
        self.login(self.sam)
        yesterday = self.today - timedelta(days=1)
        self.entry(yesterday, written=yesterday)
        self.assertEqual(self.labour(yesterday).status_code, 302)
        old = self.old_entry()
        r = self.labour(old.entry_date)
        self.assertRedirects(r, old.get_absolute_url(), fetch_redirect_response=False)
        with org_context(self.org_a):
            self.assertFalse(old.labour.exists())
            self.assertEqual(DiaryEntry.objects.get(pk=old.pk).labour.count(), 0)
        # The edit form and removing a line are refused too
        r = self.client.post(self.day_url('diary_edit', old.entry_date), {'notes': 'sneaky', 'own_staff_count': 0})
        with org_context(self.org_a):
            self.assertEqual(DiaryEntry.objects.get(pk=old.pk).notes, '')

    def test_missing_past_day_can_be_written_late(self):
        self.login(self.sam)
        day = self.today - timedelta(days=6)
        self.assertEqual(self.labour(day).status_code, 302)
        with org_context(self.org_a):
            e = DiaryEntry.objects.get(project=self.pa, entry_date=day)
            self.assertTrue(e.written_late)
            self.assertFalse(e.locked)

    def test_future_day_is_404(self):
        self.login(self.sam)
        self.assertEqual(self.client.get(self.day_url('diary_day', self.today + timedelta(days=1))).status_code, 404)

    def test_unlock_needs_pm_or_admin_and_a_reason_and_is_audited(self):
        old = self.old_entry()
        url = self.day_url('diary_lock', old.entry_date)
        self.login(self.sam)  # on the team but not the PM
        self.assertEqual(self.client.post(url, {'action': 'unlock', 'reason': 'typo'}).status_code, 403)
        self.login(self.pat)
        self.client.post(url, {'action': 'unlock', 'reason': ''})
        with org_context(self.org_a):
            self.assertTrue(DiaryEntry.objects.get(pk=old.pk).locked)
        self.client.post(url, {'action': 'unlock', 'reason': 'Labour count for Lund Joinery was wrong'})
        with org_context(self.org_a):
            e = DiaryEntry.objects.get(pk=old.pk)
            self.assertFalse(e.locked)
            self.assertEqual(e.status, DiaryEntry.STATUS_UNLOCKED)
            audit = AuditEntry.objects.filter(record_idx__object_id=e.pk, action='unlock').get()
            self.assertEqual(audit.actor, self.pat)
            self.assertIn('Lund Joinery', audit.note)
            self.assertIn('unlocked_until', audit.changes)
        # Now staff on the job can correct it, and lock it again
        self.login(self.sam)
        self.assertEqual(self.labour(old.entry_date).status_code, 302)
        self.client.post(url, {'action': 'relock'})
        with org_context(self.org_a):
            self.assertTrue(DiaryEntry.objects.get(pk=old.pk).locked)

    def test_admin_can_unlock(self):
        old = self.old_entry()
        self.login(self.adam)
        self.client.post(self.day_url('diary_lock', old.entry_date), {'action': 'unlock', 'reason': 'Correction'})
        with org_context(self.org_a):
            self.assertFalse(DiaryEntry.objects.get(pk=old.pk).locked)

    def test_entry_form_change_is_diffed(self):
        e = self.entry()
        self.login(self.sam)
        r = self.client.post(self.day_url('diary_edit'), {
            'weather_am': 'RAIN', 'weather_pm': 'DRY', 'site_start': '07:30', 'site_finish': '17:00',
            'own_staff_count': '2', 'hs_incident': 'on', 'hs_notes': 'Cut hand, first aid given', 'version': e.version})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            audit = AuditEntry.objects.filter(record_idx__object_id=e.pk, action='updated').get()
        self.assertEqual(audit.changes['weather_am'], ['', 'RAIN'])
        self.assertEqual(audit.changes['site_start'], ['', '07:30'])

    def test_incident_needs_a_note_and_hours_in_order(self):
        self.login(self.sam)
        r = self.client.post(self.day_url('diary_edit'), {'hs_incident': 'on', 'own_staff_count': '0',
                                                          'site_start': '17:00', 'site_finish': '07:00'})
        self.assertContains(r, 'Say what happened')
        self.assertContains(r, 'close after it opens')

    def test_remove_line(self):
        e = self.entry()
        with org_context(self.org_a):
            line = LabourLine.objects.create(entry=e, name='X', headcount=1)
        self.login(self.sam)
        self.client.post(self.day_url('diary_line_remove', None, 'labour', line.pk))
        with org_context(self.org_a):
            self.assertFalse(LabourLine.objects.filter(pk=line.pk).exists())
            self.assertTrue(AuditEntry.objects.filter(record_idx__object_id=e.pk, action='labour removed').exists())

    def test_labour_from_appointed_package_keeps_the_name(self):
        with org_context(self.org_a):
            pkg = Package.objects.create(project=self.pa, subcontractor=self.subco, status=Package.APPOINTED)
        self.login(self.sam)
        self.client.post(self.url('diary_line_add', self.pa.pk, self.today.isoformat(), 'labour'),
                         {'package': pkg.pk, 'headcount': '4'})
        with org_context(self.org_a):
            self.assertEqual(LabourLine.objects.get().name, 'Sid Joinery')


class PhotoTests(DiaryTestBase):
    def upload(self, *files, **data):
        payload = {'taken_on': self.today.isoformat(), 'attach': 'on', 'caption': 'Progress', 'area': 'Bar',
                   'library': list(files), **data}
        return self.client.post(self.url('diary_photo_upload', self.pa.pk), payload)

    def test_several_photos_with_thumbnails_attached_to_the_day(self):
        self.login(self.sam)
        r = self.upload(jpeg('a.jpg'), jpeg('b.jpg', size=(3000, 2000)))
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            photos = list(SitePhoto.objects.all())
            self.assertEqual(len(photos), 2)
            e = DiaryEntry.objects.get(entry_date=self.today)
            self.assertTrue(all(p.entry_id == e.pk and not p.shared_with_client for p in photos))
            big = next(p for p in photos if p.original_name == 'b.jpg')
            with big.thumbnail.open('rb') as fh:
                self.assertLessEqual(max(Image.open(fh).size), 480)
            self.assertTrue(AuditEntry.objects.filter(record_idx__object_id=e.pk, action='photos added').exists())

    def test_straight_to_the_job(self):
        self.login(self.sam)
        r = self.client.post(self.url('diary_photo_upload', self.pa.pk), {
            'taken_on': self.today.isoformat(), 'library': [jpeg()]})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.assertIsNone(SitePhoto.objects.get().entry)
            self.assertFalse(DiaryEntry.objects.exists())

    def test_validation(self):
        self.login(self.sam)
        bad = [SimpleUploadedFile('fake.jpg', b'not a picture at all', content_type='image/jpeg'),
               SimpleUploadedFile('run.exe', b'MZ...', content_type='application/octet-stream'),
               SimpleUploadedFile('doc.pdf', b'%PDF-1.4', content_type='application/pdf')]
        for f in bad:
            with self.subTest(f.name):
                r = self.upload(f)
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, 'errorlist')
        r = self.client.post(self.url('diary_photo_upload', self.pa.pk), {'taken_on': self.today.isoformat()})
        self.assertContains(r, 'at least one photo')
        with org_context(self.org_a):
            self.assertFalse(SitePhoto.objects.exists())

    def test_cannot_attach_to_a_locked_day(self):
        old = self.old_entry()
        self.login(self.sam)
        r = self.upload(jpeg(), taken_on=old.entry_date.isoformat())
        self.assertContains(r, 'locked')
        with org_context(self.org_a):
            self.assertFalse(SitePhoto.objects.exists())

    def test_upload_form_opens_the_camera(self):
        self.login(self.sam)
        r = self.client.get(self.url('diary_photo_upload', self.pa.pk))
        self.assertContains(r, 'capture="environment"')
        self.assertContains(r, 'accept="image/*"')
        self.assertContains(r, 'multiple')

    def test_sharing_is_recorded(self):
        p = self.photo(entry=self.entry())
        self.login(self.pat)
        self.client.post(self.url('diary_photo', self.pa.pk, p.pk), {
            'caption': 'Bar', 'area': 'Bar', 'taken_on': self.today.isoformat(), 'shared_with_client': 'on'})
        with org_context(self.org_a):
            p.refresh_from_db()
            self.assertTrue(p.shared_with_client)
            self.assertEqual(p.shared_by, self.pat)
            self.assertTrue(AuditEntry.objects.filter(action='photo shared').exists())

    def test_shared_photo_visibility_per_role(self):
        shared, internal = self.photo(shared=True), self.photo(shared=False)
        f = lambda p, size='thumb': self.url('diary_photo_file', self.pa.pk, p.pk, size)  # noqa: E731
        expect = [
            # (who, shared thumb, shared display, shared original, internal thumb)
            ('alice', 200, 200, 200, 200), ('adam', 200, 200, 200, 200), ('pat', 200, 200, 200, 200),
            ('sam', 200, 200, 200, 200), ('stan', 403, 403, 403, 403), ('eve', 404, 404, 404, 404),
            ('carla', 200, 200, 404, 404), ('connie', 404, 404, 404, 404), ('sid', 404, 404, 404, 404),
            ('bea', 403, 403, 403, 403), ('nora', 404, 404, 404, 404), ('bob', 404, 404, 404, 404),
        ]
        for who, st, sd, so, it in expect:
            with self.subTest(who):
                user = getattr(self, who)
                got = (self.status_for(user, f(shared)), self.status_for(user, f(shared, 'display')),
                       self.status_for(user, f(shared, 'original')), self.status_for(user, f(internal)))
                self.assertEqual(got, (st, sd, so, it))

    def test_client_overview_shows_only_shared_photos(self):
        shared, internal = self.photo(shared=True), self.photo(shared=False)
        self.login(self.carla)
        r = self.client.get(self.url('project_detail', self.pa.pk))
        self.assertContains(r, 'Site photos')
        self.assertContains(r, shared.thumb_url)
        self.assertNotContains(r, internal.thumb_url)
        self.assertNotContains(r, 'Today on site')
        for who in (self.sid, self.connie):
            self.login(who)
            r = self.client.get(self.url('project_detail', self.pa.pk))
            self.assertNotContains(r, 'Site photos')
            self.assertNotContains(r, shared.thumb_url)

    def test_gallery_filters(self):
        self.photo()
        with org_context(self.org_a):
            p = SitePhoto(project=self.pa, area='Kitchen', taken_on=self.today - timedelta(days=3), uploaded_by=self.pat)
            attach(p, jpeg())
            p.save()
        self.login(self.pat)
        r = self.client.get(self.url('diary_gallery', self.pa.pk) + '?area=Kitchen')
        self.assertEqual(len(r.context['photos']), 1)
        r = self.client.get(self.url('diary_gallery', self.pa.pk) + f'?date={self.today.isoformat()}')
        self.assertEqual(len(r.context['photos']), 1)
        r = self.client.get(self.url('diary_gallery', self.pa.pk) + '?shared=1')
        self.assertContains(r, 'No photos match')

    def test_photo_on_locked_day_cannot_be_deleted(self):
        old = self.old_entry()
        p = self.photo(entry=old)
        self.login(self.pat)
        self.client.post(self.url('diary_photo', self.pa.pk, p.pk), {'action': 'delete'})
        with org_context(self.org_a):
            self.assertTrue(SitePhoto.objects.filter(pk=p.pk).exists())
        loose = self.photo()
        self.client.post(self.url('diary_photo', self.pa.pk, loose.pk), {'action': 'delete'})
        with org_context(self.org_a):
            self.assertFalse(SitePhoto.objects.filter(pk=loose.pk).exists())


class AccessTests(DiaryTestBase):
    PAGES = ('diary_register', 'diary_today', 'diary_delays', 'diary_gallery', 'diary_photo_upload')

    def test_who_can_open_the_diary(self):
        expect = {'alice': 200, 'adam': 200, 'pat': 200, 'sam': 200, 'stan': 403, 'eve': 404, 'carla': 403,
                  'connie': 403, 'sid': 403, 'bea': 403, 'nora': 404, 'bob': 404, None: 302}
        for who, code in expect.items():
            user = getattr(self, who) if who else None
            for name in self.PAGES:
                with self.subTest(who=who, page=name):
                    got = self.status_for(user, self.url(name, self.pa.pk))
                    self.assertEqual(got, 302 if name == 'diary_today' and code == 200 else code)

    def test_day_pages_and_writes_refused_to_others(self):
        e = self.entry()
        for who in ('stan', 'carla', 'sid'):
            with self.subTest(who):
                user = getattr(self, who)
                self.assertEqual(self.status_for(user, self.day_url('diary_day')), 403)
                self.assertEqual(self.status_for(user, self.day_url('diary_pdf')), 403)
                self.assertEqual(self.status_for(user, self.url('diary_line_add', self.pa.pk, self.today.isoformat(),
                                                                'labour'), 'post', {'name': 'x', 'headcount': 1}), 403)
        with org_context(self.org_a):
            self.assertFalse(e.labour.exists())

    def test_tab_only_for_staff_on_the_job(self):
        for who, shown in (('pat', True), ('alice', True), ('sam', True), ('stan', False), ('carla', False)):
            with self.subTest(who):
                self.login(getattr(self, who))
                r = self.client.get(self.url('project_detail', self.pa.pk))
                self.assertEqual(self.url('diary_register', self.pa.pk) in r.content.decode(), shown)

    def test_archived_job_is_read_only(self):
        e = self.entry()
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.url('diary_register', self.pa.pk)).status_code, 200)
        r = self.client.post(self.url('diary_line_add', self.pa.pk, self.today.isoformat(), 'labour'),
                             {'name': 'x', 'headcount': 1})
        self.assertEqual(r.status_code, 403)
        with org_context(self.org_a):
            self.assertFalse(e.labour.exists())

    def test_settings_admin_only(self):
        self.assertEqual(self.status_for(self.pat, self.url('diary_settings')), 403)
        self.login(self.adam)
        self.client.post(self.url('diary_settings'), {'saturday_counts': 'on'})
        self.assertTrue(DiarySettings.for_org(self.org_a).saturday_counts)
        self.assertFalse(DiarySettings.for_org(self.org_b).saturday_counts)


class ListTests(DiaryTestBase):
    def test_register_totals_and_filters(self):
        e = self.entry()
        old = self.old_entry()
        with org_context(self.org_a):
            LabourLine.objects.create(entry=e, name='A', headcount=4)
            DelayLine.objects.create(entry=old, cause='WEATHER', hours_lost=Decimal('3'), description='Rain')
        self.photo(entry=e)
        self.login(self.pat)
        r = self.client.get(self.url('diary_register', self.pa.pk))
        rows = {x.pk: x for x in r.context['entries']}
        self.assertEqual((rows[e.pk].total_labour, rows[e.pk].photo_count), (4, 1))
        self.assertEqual((rows[old.pk].delay_count, rows[old.pk].delay_hours), (1, Decimal('3')))
        self.assertEqual([x.pk for x in self.client.get(self.url('diary_register', self.pa.pk) + '?delays=1')
                          .context['entries']], [old.pk])
        self.assertEqual([x.pk for x in self.client.get(self.url('diary_register', self.pa.pk) + '?lock=locked')
                          .context['entries']], [old.pk])
        self.assertContains(self.client.get(self.url('diary_register', self.pa.pk) + '?incident=1'),
                            'No diary entries match')

    def test_empty_register_explains(self):
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('diary_register', self.pa.pk)), 'No diary entries yet')
        self.assertContains(self.client.get(self.url('diary_delays', self.pa.pk)), 'No delays logged')

    def test_delays_list_and_csv(self):
        old = self.old_entry()
        with org_context(self.org_a):
            DelayLine.objects.create(entry=old, cause='INFORMATION', hours_lost=Decimal('3'),
                                     description='=HYPERLINK("x") stood down', created_by=self.pat)
            DelayLine.objects.create(entry=self.entry(), cause='WEATHER', hours_lost=Decimal('1.5'), description='Wind')
        self.login(self.pat)
        r = self.client.get(self.url('diary_delays', self.pa.pk))
        self.assertEqual(r.context['total'], Decimal('4.5'))
        r = self.client.get(self.url('diary_delays', self.pa.pk) + '?cause=WEATHER')
        self.assertEqual(r.context['total'], Decimal('1.5'))
        r = self.client.get(self.url('diary_delays', self.pa.pk) + '?export=csv')
        lines = r.content.decode().splitlines()
        self.assertEqual(lines[0], 'Date,Diary ref,Cause,Hours lost,What happened,Trades affected,Logged by,Logged at')
        self.assertIn("'=HYPERLINK", r.content.decode())  # no formulas in the spreadsheet

    def test_pdf(self):
        e = self.entry()
        with org_context(self.org_a):
            DelayLine.objects.create(entry=e, cause='OTHER', hours_lost=1, description='<img src="/etc/passwd"> & <b>')
        self.login(self.pat)
        r = self.client.get(self.day_url('diary_pdf'))
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_site_mode_is_phone_first(self):
        self.login(self.sam)
        r = self.client.get(self.url('diary_today', self.pa.pk), follow=True)
        html = r.content.decode()
        self.assertIn('class="site-mode touch"', html)
        self.assertIn('class="tap-grid"', html)
        for label in ('Add labour', 'Add delivery', 'Add visitor', 'Log a delay', 'Take photos', 'Raise a snag'):
            self.assertIn(label, html)
        self.assertIn(self.url('issue_create', self.pa.pk), html)
        self.assertIn('diary/diary.css', html)
        # The stylesheet holds the 44px rule for .touch
        from django.contrib.staticfiles import finders
        with open(finders.find('diary/diary.css')) as fh:
            self.assertIn('min-height: 44px', fh.read())
        # And the overview links to it prominently
        r = self.client.get(self.url('project_detail', self.pa.pk))
        self.assertContains(r, 'today-link')

    def test_locked_day_shows_no_buttons(self):
        old = self.old_entry()
        self.login(self.sam)
        r = self.client.get(old.get_absolute_url())
        self.assertNotContains(r, 'class="tap-grid"')
        self.assertContains(r, 'ask the project manager to unlock it')
        self.login(self.pat)
        self.assertContains(self.client.get(old.get_absolute_url()), 'Unlock for correction')


class NeedsMeTests(DiaryTestBase):
    """Wed 28 Oct, Thu 29, Fri 30, Sat 31 Oct, Sun 1 Nov, Mon 2 Nov 2026."""

    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.pa.start_date = date(2026, 10, 26)
            self.pa.save()
            for d in (date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)):
                DiaryEntry(project=self.pa, entry_date=d).save(actor=self.pat)

    def missing(self, now, user=None):
        with org_context(self.org_a):
            return [i.key.split(':')[-1] for i in items(self.request_for(user or self.pat), now=now)]

    def test_due_by_ten_next_working_day(self):
        self.assertEqual(self.missing(datetime(2026, 10, 30, 9, 59)), [])
        self.assertEqual(self.missing(datetime(2026, 10, 30, 10, 0)), ['2026-10-29'])

    def test_weekend_excluded(self):
        # Friday's entry isn't chased over the weekend, only from Monday 10:00
        self.assertEqual(self.missing(datetime(2026, 11, 1, 12, 0)), ['2026-10-29'])
        self.assertEqual(self.missing(datetime(2026, 11, 2, 9, 0)), ['2026-10-29'])
        self.assertEqual(self.missing(datetime(2026, 11, 2, 10, 0)), ['2026-10-30', '2026-10-29'])

    def test_saturday_counts_when_the_company_says_so(self):
        with org_context(self.org_a):
            DiarySettings.objects.create(saturday_counts=True)
        self.assertEqual(self.missing(datetime(2026, 11, 1, 12, 0)), ['2026-10-30', '2026-10-29'])
        self.assertEqual(self.missing(datetime(2026, 11, 2, 10, 0)), ['2026-10-31', '2026-10-30', '2026-10-29'])

    def test_only_the_pm_of_a_live_job(self):
        now = datetime(2026, 10, 30, 10, 0)
        for who in (self.adam, self.alice, self.sam, self.carla):
            self.assertEqual(self.missing(now, who), [])
        with org_context(self.org_a):
            self.pa.stage = self.pa.STAGE_WON
            self.pa.save()
        self.assertEqual(self.missing(now), [])

    def test_no_start_date_no_items(self):
        with org_context(self.org_a):
            self.pa.start_date = None
            self.pa.save()
        self.assertEqual(self.missing(datetime(2026, 10, 30, 10, 0)), [])

    def test_item_shape_and_registry(self):
        with org_context(self.org_a):
            item = items(self.request_for(self.pat), now=datetime(2026, 10, 30, 10, 0))[0]
        self.assertEqual(item.due, date(2026, 10, 30))
        self.assertEqual(item.url, self.url('diary_day', self.pa.pk, '2026-10-29'))
        self.assertIn('diary_missing', dict(actions.kinds()))

    def test_written_entry_clears_it(self):
        with org_context(self.org_a):
            DiaryEntry(project=self.pa, entry_date=date(2026, 10, 29)).save(actor=self.sam)
        self.assertEqual(self.missing(datetime(2026, 10, 30, 10, 0)), [])


class TenancyTests(DiaryTestBase):
    def test_other_company_gets_404(self):
        e = self.entry()
        p = self.photo(shared=True, entry=e)
        self.login(self.bob)
        for url in (self.url('diary_register', self.pa.pk), self.day_url('diary_day'),
                    self.url('diary_photo_file', self.pa.pk, p.pk, 'thumb'), self.url('diary_photo', self.pa.pk, p.pk)):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 404)
        # Bob's own project can't reach Alpha's photo by id either
        self.assertEqual(self.client.get(self.url('diary_photo_file', self.pb.pk, p.pk, 'thumb', org=self.org_b))
                         .status_code, 404)
        with org_context(self.org_b):
            self.assertFalse(DiaryEntry.objects.exists())
            self.assertFalse(SitePhoto.objects.exists())

    def test_no_organisation_fails_closed(self):
        self.entry()
        from ..models import DelayLine as DL, DeliveryLine, InstructionLine, PlantLine, VisitorLine
        for model in (DiaryEntry, SitePhoto, LabourLine, DL, DeliveryLine, InstructionLine, PlantLine, VisitorLine,
                      DiarySettings):
            with self.subTest(model.__name__), self.assertRaises(TenantContextMissing):
                list(model.objects.all())
