"""
The fire-stopping register through the views: adding a penetration (only "where" is required), photos
before and after, sealing, sign-off (needs the system and an after photo), failing (needs a note),
resealing, reopening (PM only), the CSV export, Needs me, and pins from the drawing viewer.
"""
from django.core.files.base import ContentFile
from django.test import override_settings

from core.workflow import apply_transition
from projects.models import Document, DocumentRevision
from tenancy.context import org_context

from ..exports import HEADER, register
from ..models import Penetration, QualityPhoto
from .base import MEDIA, QualityTestBase, jpeg, tearDownModule  # noqa: F401


@override_settings(MEDIA_ROOT=MEDIA)
class FirestopFlowTests(QualityTestBase):
    def test_empty_register_says_what_goes_here(self):
        self.login(self.pat)
        r = self.client.get(self.purl('penetration_list'))
        self.assertContains(r, 'No fire-stopping recorded yet')
        self.assertContains(r, 'Add a penetration')

    def test_only_where_is_required(self):
        self.login(self.sam)
        r = self.client.post(self.purl('penetration_create'), {'element': 'WALL'})
        self.assertContains(r, 'Say where it is: level, room and wall.')
        r = self.client.post(self.purl('penetration_create'), {'location': 'L2 riser', 'element': 'WALL'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            p = Penetration.objects.get()
        self.assertEqual((p.ref, p.status, p.title), ('FS-001', Penetration.TO_SEAL, 'Penetration through a wall'))

    def test_add_with_photos_marks_it_sealed_for_the_pm(self):
        self.login(self.sam)
        r = self.client.post(self.purl('penetration_create'), {
            'location': 'L1 kitchen wall', 'element': 'WALL', 'fire_rating': '60', 'service': 'PIPE_PLASTIC',
            'size': '110 mm', 'system': 'Intumescent collar', 'package': self.pkg.pk,
            'before_photo': jpeg('before.jpg'), 'after_photo': jpeg('after.jpg')})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            p = Penetration.objects.get()
            self.assertEqual(sorted(QualityPhoto.objects.values_list('stage', flat=True)), ['AFTER', 'BEFORE'])
        self.assertEqual(p.title, '110 mm plastic pipe through a 60 minute wall')
        self.assertEqual((p.status, p.ball_in_court_user, p.installed_by), (Penetration.SEALED, self.pat,
                                                                           'Sid Joinery'))
        self.assertIsNotNone(p.installed_on)

    def test_sign_off_needs_the_system_and_an_after_photo(self):
        p = self.penetration()
        self.login(self.sam)  # whoever seals it doesn't sign it off (QualitySettings.fs_independent_sign_off)
        self.client.post(self.purl('penetration_action', p.pk), {'action': 'seal'})
        self.assertEqual(self.fresh(p).status, Penetration.SEALED)
        self.login(self.pat)
        r = self.client.post(self.purl('penetration_action', p.pk), {'action': 'sign_off'}, follow=True)
        self.assertContains(r, 'Record the fire-stopping system or product')
        with org_context(self.org_a):
            Penetration.objects.filter(pk=p.pk).update(system='Fire batt')
        r = self.client.post(self.purl('penetration_action', p.pk), {'action': 'sign_off'}, follow=True)
        self.assertContains(r, 'Add a photo of the finished seal')
        self.client.post(self.purl('penetration_photo', p.pk), {'stage': 'AFTER', 'photo': jpeg()})
        self.client.post(self.purl('penetration_action', p.pk), {'action': 'sign_off', 'note': 'As detail FB-2'})
        p = self.fresh(p)
        self.assertEqual((p.status, p.inspected_by, p.inspection_note, p.ball_in_court_user),
                         (Penetration.SIGNED_OFF, self.pat, 'As detail FB-2', None))

    def test_fail_needs_a_note_then_reseal(self):
        p = self.penetration(actor=self.sam, system='Sealant')
        with org_context(self.org_a):
            apply_transition(p, 'seal', self.sam)
        self.login(self.pat)
        r = self.client.post(self.purl('penetration_action', p.pk), {'action': 'fail'}, follow=True)
        self.assertContains(r, 'Say what is wrong with the seal')
        self.client.post(self.purl('penetration_action', p.pk), {'action': 'fail', 'note': 'Gap behind cables'})
        p = self.fresh(p)
        self.assertEqual((p.status, p.ball_in_court_user, p.inspection_note), (Penetration.FAILED, self.sam,
                                                                               'Gap behind cables'))
        self.login(self.sam)
        self.client.post(self.purl('penetration_action', p.pk), {'action': 'reseal'})
        p = self.fresh(p)
        self.assertEqual((p.status, p.ball_in_court_user), (Penetration.SEALED, self.pat))

    def test_signed_off_is_locked_and_only_the_pm_reopens(self):
        p = self.penetration(system='Collar')
        with org_context(self.org_a):
            from ..photos import add_photo
            add_photo(self.pa, self.pat, jpeg(), penetration=p, stage='AFTER')
            apply_transition(p, 'seal', self.sam)
            apply_transition(p, 'sign_off', self.pat)
            photo = QualityPhoto.objects.get()
        self.login(self.sam)
        r = self.client.post(self.purl('penetration_photo', p.pk), {'stage': 'AFTER', 'photo': jpeg()}, follow=True)
        self.assertContains(r, 'is signed off')
        r = self.client.post(self.purl('quality_photo_remove', photo.pk))
        self.assertEqual(r.status_code, 403)  # not theirs
        self.login(self.pat)
        r = self.client.post(self.purl('quality_photo_remove', photo.pk), follow=True)
        self.assertContains(r, 'part of the record')
        self.login(self.sam)
        self.assertEqual(self.client.post(self.purl('penetration_action', p.pk),
                                          {'action': 'reopen', 'note': 'x'}).status_code, 403)
        self.login(self.pat)
        self.client.post(self.purl('penetration_action', p.pk), {'action': 'reopen', 'note': 'Cut out for new cable'})
        self.assertEqual(self.fresh(p).status, Penetration.TO_SEAL)

    def test_edit_keeps_the_title_in_step(self):
        p = self.penetration()
        self.login(self.pat)
        r = self.client.post(self.purl('penetration_edit', p.pk), {
            'location': 'L1 riser wall', 'element': 'FLOOR', 'fire_rating': '120', 'service': 'DUCT',
            'version': p.version})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.fresh(p).title, 'Ductwork through a 120 minute floor')
        # A stale form is refused
        r = self.client.post(self.purl('penetration_edit', p.pk), {
            'location': 'Elsewhere', 'element': 'WALL', 'version': p.version})
        self.assertContains(r, 'Someone else changed this')

    def test_register_filters_search_and_csv(self):
        a = self.penetration(location='=1+1 core', system='Collar')
        self.penetration(location='L3 riser', fire_rating=120)
        with org_context(self.org_a):
            apply_transition(a, 'seal', self.pat)
        self.login(self.pat)
        r = self.client.get(self.purl('penetration_list') + '?status=to_inspect')
        self.assertContains(r, '1+1 core')
        self.assertNotContains(r, 'L3 riser')
        r = self.client.get(self.purl('penetration_list') + '?q=riser&rating=120')
        self.assertContains(r, 'L3 riser')
        r = self.client.get(self.purl('penetration_list') + '?export=csv')
        body = r.content.decode()
        self.assertTrue(body.startswith('Ref,Where,Through'))
        self.assertIn("'=1+1 core", body)
        self.assertIn('FS-002', body)

    def test_register_rows_for_the_golden_thread(self):
        self.penetration(system='Collar', size='110 mm')
        with org_context(self.org_a):
            header, rows = register(self.pa)
        self.assertEqual(header, HEADER)
        self.assertEqual(len(rows[0]), len(HEADER))
        self.assertEqual(rows[0][0], 'FS-001')


@override_settings(MEDIA_ROOT=MEDIA)
class FirestopNeedsMeTests(QualityTestBase):
    def items(self, user):
        import actions
        from tenancy.models import Membership
        m = Membership.objects.get(organization=self.org_a, user=user)
        with org_context(self.org_a):
            return [i for i in actions.for_user(actions.request_for(user, m)) if i.kind == 'firestopping']

    def test_sealed_goes_to_the_pm_failed_back_to_who_recorded_it(self):
        p = self.penetration(actor=self.sam)
        self.assertEqual(self.items(self.pat), [])
        with org_context(self.org_a):
            apply_transition(p, 'seal', self.sam)
        self.assertEqual([i.action for i in self.items(self.pat)], ['Inspect the seal and sign it off'])
        with org_context(self.org_a):
            apply_transition(p, 'fail', self.pat, remark='Gap')
        self.assertEqual(self.items(self.pat), [])
        self.assertEqual([i.ref for i in self.items(self.sam)], ['FS-001'])
        for user in (self.carla, self.sid, self.stan):
            self.assertEqual(self.items(user), [])


@override_settings(MEDIA_ROOT=MEDIA)
class ViewerPinTests(QualityTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            self.doc = Document.objects.create(project=self.pa, title='Ground floor GA', reference='GA-01',
                                               category='DRAWING', visibility=Document.VIS_SHARED)
            self.rev = self.doc.add_revision(DocumentRevision(
                revision='C01', file=ContentFile(b'%PDF-1.4 test', name='ga.pdf'), original_name='ga.pdf',
                size=12, uploaded_by=self.pat, suitability='A1'))

    def pin_query(self):
        return f'?pin_doc={self.doc.pk}&pin_rev={self.rev.pk}&pin_page=1&pin_x=0.25&pin_y=0.75'

    def test_new_penetration_from_the_viewer_is_pinned_and_shown(self):
        self.login(self.pat)
        url = self.purl('penetration_create') + self.pin_query()
        r = self.client.get(url)
        self.assertContains(r, 'It will be pinned on drawing')
        r = self.client.post(url, {'location': 'Drawing GA-01, by the bar', 'element': 'WALL'})
        with org_context(self.org_a):
            p = Penetration.objects.get()
        self.assertEqual((p.drawing, p.drawing_revision, p.pin_page, str(p.pin_x), str(p.pin_y)),
                         (self.doc, self.rev, 1, '0.250000', '0.750000'))
        self.assertIn(f'?pin=fs-{p.pk}', r['Location'])
        viewer = self.client.get(self.url('drawing_viewer', self.pa.pk, self.doc.pk, self.rev.pk))
        self.assertContains(viewer, f'fs-{p.pk}')
        self.assertContains(viewer, 'New fire-stopping')
        self.assertContains(self.client.get(p.get_absolute_url()), 'Show on drawing')

    def test_a_bad_pin_is_ignored_not_trusted(self):
        self.login(self.pat)
        self.client.post(self.purl('penetration_create') + f'?pin_doc={self.doc.pk}&pin_rev={self.rev.pk}'
                         '&pin_page=1&pin_x=7&pin_y=0.5', {'location': 'L1', 'element': 'WALL'})
        with org_context(self.org_a):
            self.assertIsNone(Penetration.objects.get().drawing_id)

    def test_externals_never_see_fire_stopping_pins_or_the_button(self):
        p = self.penetration()
        with org_context(self.org_a):
            Penetration.objects.filter(pk=p.pk).update(drawing=self.doc, drawing_revision=self.rev, pin_page=1,
                                                       pin_x='0.5', pin_y='0.5')
            from documents.models import Transmittal  # noqa: F401 - documents app loaded
            self.rev.shared_at = self.rev.uploaded_at
            self.rev.save()
        self.login(self.carla)
        r = self.client.get(self.url('drawing_viewer', self.pa.pk, self.doc.pk, self.rev.pk))
        if r.status_code == 200:
            self.assertNotContains(r, f'fs-{p.pk}')
            self.assertNotContains(r, 'New fire-stopping')
