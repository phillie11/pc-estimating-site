"""
Inspections through the views: the library, starting one, answering checks with notes and photos,
raising a snag in one tap, finishing, reopening and cancelling, numbering, the index and the audit trail.
"""
from django.core import mail

from core.models import AuditEntry, RecordIndex
from projects.models import Issue, ProjectMember
from tenancy.context import org_context

from .. import rules
from ..library import DEFAULT_TEMPLATES, ensure_library
from ..models import ChecklistTemplate, Inspection, InspectionItem, QualityPhoto, QualitySettings, TemplateItem
from .base import QualityTestBase, jpeg


class LibraryTests(QualityTestBase):
    def test_library_is_seeded_once_per_company(self):
        with org_context(self.org_a):
            self.assertEqual(ChecklistTemplate.objects.count(), len(DEFAULT_TEMPLATES))
            names = set(ChecklistTemplate.objects.values_list('name', flat=True))
            self.assertFalse(ensure_library(self.org_a))
            self.assertEqual(ChecklistTemplate.objects.count(), len(DEFAULT_TEMPLATES))
        for trade in ('Partitions: before boarding', 'Suspended ceilings', 'M&E first fix', 'M&E second fix',
                      'Fire doors', 'Decorations'):
            self.assertIn(trade, names)
        with org_context(self.org_b):
            self.assertEqual(ChecklistTemplate.objects.count(), 0)  # Bravo gets its own on first use
            self.assertTrue(ensure_library(self.org_b))
            self.assertTrue(QualitySettings.objects.get().library_seeded)

    def test_staff_add_and_edit_a_checklist(self):
        self.login(self.pat)
        r = self.client.post(self.url('quality_template_create'), {
            'name': 'Vinyl flooring', 'trade': 'Flooring', 'description': '',
            'checks': 'Subfloor dry (moisture reading)\n- Adhesive as the spec\n\nWelds flush'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            t = ChecklistTemplate.objects.get(name='Vinyl flooring')
            self.assertEqual([i.text for i in t.items.all()],
                             ['Subfloor dry (moisture reading)', 'Adhesive as the spec', 'Welds flush'])
        r = self.client.post(self.url('quality_template', t.pk), {
            'name': 'Vinyl flooring', 'trade': 'Flooring', 'checks': 'Only one check', 'is_active': 'on'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            self.assertEqual(TemplateItem.objects.filter(template=t).count(), 1)

    def test_empty_checks_are_refused_with_how_to_fix(self):
        self.login(self.pat)
        r = self.client.post(self.url('quality_template_create'), {'name': 'Nothing', 'checks': '\n \n'})
        self.assertContains(r, 'Type at least one check, one per line.')

    def test_estimating_staff_can_read_but_not_change_the_library(self):
        self.login(self.eve)
        self.assertEqual(self.client.get(self.url('quality_templates')).status_code, 200)
        self.assertEqual(self.client.get(self.url('quality_template_create')).status_code, 403)
        self.assertEqual(self.client.post(self.url('quality_template', self.template.pk),
                                          {'name': 'Hacked', 'checks': 'x'}).status_code, 403)

    def test_externals_never_see_the_library(self):
        for user in (self.carla, self.sid):
            self.login(user)
            self.assertEqual(self.client.get(self.url('quality_templates')).status_code, 403)

    def test_settings_are_owners_and_admins_only(self):
        self.login(self.pat)
        self.assertIn(self.client.get(self.url('quality_settings')).status_code, (302, 403))
        self.login(self.adam)
        r = self.client.post(self.url('quality_settings'),
                             {'permit_hours': 10, 'expiry_warning_hours': 3, 'fire_watch_minutes': 120})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            s = QualitySettings.for_org(self.org_a)
        self.assertEqual((s.permit_hours, s.expiry_warning_hours, s.fire_watch_minutes), (10, 3, 120))


class InspectionFlowTests(QualityTestBase):
    def test_empty_list_says_what_goes_here(self):
        self.login(self.pat)
        r = self.client.get(self.purl('inspection_list'))
        self.assertContains(r, 'No inspections yet')
        self.assertContains(r, 'Start an inspection')

    def test_start_copies_the_checks_and_defaults_to_me_today(self):
        self.login(self.sam)
        r = self.client.get(self.purl('inspection_create'))
        self.assertContains(r, f'<option value="{self.sam.pk}" selected>')
        r = self.client.post(self.purl('inspection_create'), {
            'template': self.template.pk, 'area': 'Level 1 corridor', 'package': self.pkg.pk,
            'ball_in_court_user': self.sam.pk, 'due_date': '2026-11-02'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            ins = Inspection.objects.get()
            self.assertEqual(ins.ref, 'INS-001')
            self.assertEqual(ins.title, 'Fire doors, Level 1 corridor')
            self.assertEqual(ins.items.count(), self.template.items.count())
            self.assertEqual(ins.status, Inspection.IN_PROGRESS)
            idx = RecordIndex.objects.get(record_type='quality.inspection', object_id=ins.pk)
            self.assertEqual((idx.ref, idx.ball_in_court_user_id), ('INS-001', self.sam.pk))
            # Changing the library later never touches the inspection's checks
            self.template.items.all().delete()
            self.assertEqual(ins.items.count(), 8)

    def test_only_a_checklist_is_required(self):
        self.login(self.pat)
        r = self.client.post(self.purl('inspection_create'), {'ball_in_court_user': self.pat.pk})
        self.assertContains(r, 'Choose the checklist to inspect against.')

    def test_assigning_someone_else_tells_them(self):
        self.login(self.pat)
        self.client.post(self.purl('inspection_create'), {'template': self.template.pk,
                                                          'ball_in_court_user': self.sam.pk})
        self.assertTrue(any('inspect' in m.subject.lower() for m in mail.outbox))

    def test_answer_checks_with_note_and_photo(self):
        ins, items = self.inspection()
        self.login(self.pat)
        url = self.purl('inspection_item', ins.pk, items[0].pk)
        r = self.client.post(url, {'result': 'FAIL', 'note': 'Gap 6 mm', 'photo': jpeg()})
        self.assertRedirects(r, ins.get_absolute_url() + f'#check-{items[0].pk}', fetch_redirect_response=False)
        item = self.fresh(items[0])
        self.assertEqual((item.result, item.note, item.answered_by_id), ('FAIL', 'Gap 6 mm', self.pat.pk))
        with org_context(self.org_a):
            photo = QualityPhoto.objects.get(item=item)
        self.assertTrue(photo.thumbnail and photo.display)
        self.assertEqual(self.client.get(self.purl('quality_photo_file', photo.pk, 'thumb')).status_code, 200)
        # Saving a note alone keeps the result
        self.client.post(url, {'result': '', 'note': 'Gap 6 mm at the head'})
        self.assertEqual(self.fresh(items[0]).result, 'FAIL')

    def test_a_file_that_is_not_a_picture_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        ins, items = self.inspection()
        self.login(self.pat)
        bad = SimpleUploadedFile('photo.jpg', b'not a picture', content_type='image/jpeg')
        r = self.client.post(self.purl('inspection_item', ins.pk, items[0].pk), {'result': 'PASS', 'photo': bad},
                             follow=True)
        self.assertContains(r, 'could not be read as a picture')
        self.assertEqual(self.fresh(items[0]).result, '')

    def test_raise_a_snag_from_a_failed_check_in_one_tap(self):
        ins, items = self.inspection(package=self.pkg)
        self.login(self.pat)
        self.client.post(self.purl('inspection_item', ins.pk, items[1].pk),
                         {'result': 'FAIL', 'note': 'Closer does not latch', 'photo': jpeg()})
        r = self.client.post(self.purl('inspection_snag', ins.pk, items[1].pk))
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            snag = Issue.objects.get()
            self.assertEqual(snag.issue_type, 'QUALITY')
            self.assertEqual(snag.assignee, self.sid)  # the subcontractor on the inspection's package
            self.assertIn('Closer does not latch', snag.description)
            self.assertIn(ins.ref, snag.description)
            self.assertTrue(snag.photo)
            self.assertEqual(self.fresh(items[1]).snag_id, snag.pk)
        # A second tap doesn't raise a second snag
        self.client.post(self.purl('inspection_snag', ins.pk, items[1].pk))
        with org_context(self.org_a):
            self.assertEqual(Issue.objects.count(), 1)
        # The sub sees it under their snags
        self.login(self.sid)
        self.assertContains(self.client.get(self.url('issue_list', self.pa.pk)), 'Failed check')

    def test_a_snag_needs_a_failed_check(self):
        ins, items = self.inspection(results=['PASS'])
        self.login(self.pat)
        r = self.client.post(self.purl('inspection_snag', ins.pk, items[0].pk), follow=True)
        self.assertContains(r, 'Only a failed check can raise a snag')
        with org_context(self.org_a):
            self.assertFalse(Issue.objects.exists())

    def test_without_a_package_the_snag_goes_to_the_inspector(self):
        ins, items = self.inspection(results=['FAIL'], inspector=self.sam)
        self.login(self.pat)
        self.client.post(self.purl('inspection_snag', ins.pk, items[0].pk))
        with org_context(self.org_a):
            self.assertEqual(Issue.objects.get().assignee, self.sam)

    def test_finish_needs_every_check_answered(self):
        ins, items = self.inspection(results=['PASS'] * 7)
        self.login(self.pat)
        r = self.client.post(self.purl('inspection_action', ins.pk), {'action': 'finish'}, follow=True)
        self.assertContains(r, '1 check still to answer')
        self.assertEqual(self.fresh(ins).status, Inspection.IN_PROGRESS)

    def test_finish_passes_or_fails_and_clears_needs_me(self):
        ins, _ = self.inspection(results=['PASS'] * 7 + ['NA'])
        self.login(self.pat)
        self.client.post(self.purl('inspection_action', ins.pk), {'action': 'finish', 'notes': 'All good'})
        ins = self.fresh(ins)
        self.assertEqual((ins.status, ins.notes, ins.ball_in_court_user_id), (Inspection.PASSED, 'All good', None))
        self.assertEqual(ins.completed_by, self.pat)
        bad, _ = self.inspection(results=['PASS'] * 6 + ['FAIL', 'PASS'])
        self.client.post(self.purl('inspection_action', bad.pk), {'action': 'finish'})
        self.assertEqual(self.fresh(bad).status, Inspection.FAILED)
        with org_context(self.org_a):
            entry = AuditEntry.objects.filter(record_idx__object_id=bad.pk,
                                              record_idx__record_type='quality.inspection', action='fail').get()
            self.assertEqual(entry.note, '7 pass, 1 fail, 0 N/A')

    def test_finished_checks_cant_change_and_only_the_pm_reopens(self):
        ins, items = self.inspection(results=['PASS'] * 8)
        self.login(self.sam)
        self.client.post(self.purl('inspection_action', ins.pk), {'action': 'finish'})
        r = self.client.post(self.purl('inspection_item', ins.pk, items[0].pk), {'result': 'FAIL'}, follow=True)
        self.assertContains(r, 'can&#x27;t change')
        self.assertEqual(self.fresh(items[0]).result, 'PASS')
        self.assertEqual(self.client.post(self.purl('inspection_action', ins.pk),
                                          {'action': 'reopen', 'reason': 'x'}).status_code, 403)
        self.login(self.pat)
        r = self.client.post(self.purl('inspection_action', ins.pk), {'action': 'reopen'}, follow=True)
        self.assertContains(r, 'Say why it is being reopened')
        self.client.post(self.purl('inspection_action', ins.pk), {'action': 'reopen', 'reason': 'Door D02 replaced'})
        ins = self.fresh(ins)
        self.assertEqual((ins.status, ins.ball_in_court_user_id), (Inspection.IN_PROGRESS, self.pat.pk))

    def test_cancel(self):
        ins, _ = self.inspection()
        self.login(self.sam)
        self.client.post(self.purl('inspection_action', ins.pk), {'action': 'cancel'})
        self.assertEqual(self.fresh(ins).status, Inspection.CANCELLED)

    def test_list_filters_search_csv_and_paging(self):
        a, _ = self.inspection(title='Fire doors, Level 1')
        b, _ = self.inspection(title='Fire doors, Level 2', results=['FAIL'] * 8)
        with org_context(self.org_a):
            from core.workflow import apply_transition
            apply_transition(b, 'fail', self.pat)
        self.login(self.pat)
        r = self.client.get(self.purl('inspection_list'))
        self.assertContains(r, 'Level 1')
        self.assertNotContains(r, 'Level 2')  # "In progress" first while there are any
        r = self.client.get(self.purl('inspection_list') + '?status=failed')
        self.assertContains(r, 'Level 2')
        self.assertContains(r, '8 failed')
        r = self.client.get(self.purl('inspection_list') + '?status=all&q=level+2')
        self.assertContains(r, 'Level 2')
        self.assertNotContains(r, 'Fire doors, Level 1<')
        r = self.client.get(self.purl('inspection_list') + '?status=all&export=csv')
        self.assertEqual(r['Content-Type'], 'text/csv')
        body = r.content.decode()
        self.assertIn('INS-001', body)
        self.assertIn('INS-002', body)

    def test_csv_cells_are_formula_safe(self):
        self.inspection(area='=HYPERLINK("http://x")', title='=cmd|x')
        self.login(self.pat)
        body = self.client.get(self.purl('inspection_list') + '?status=all&export=csv').content.decode()
        self.assertIn("'=cmd|x", body)
        self.assertNotIn(',=cmd', body)

    def test_inspection_page_shows_progress_and_buttons(self):
        ins, items = self.inspection(results=['PASS', 'FAIL'])
        self.login(self.pat)
        r = self.client.get(ins.get_absolute_url())
        self.assertContains(r, '2 of 8')
        self.assertContains(r, 'accept="image/*" capture="environment"')
        self.assertContains(r, 'Raise a snag for this')
        self.assertContains(r, 'value="PASS"')


class InspectionRecordTests(QualityTestBase):
    def test_numbering_is_per_project(self):
        a, _ = self.inspection()
        b, _ = self.inspection()
        with org_context(self.org_a):
            from projects.models import Project
            other = Project.objects.create(name='Other job', stage=Project.STAGE_LIVE, manager=self.pat)
        c, _ = self.inspection(project=other)
        self.assertEqual((a.ref, b.ref, c.ref), ('INS-001', 'INS-002', 'INS-001'))

    def test_index_and_audit_follow_the_record(self):
        ins, _ = self.inspection(results=['PASS'] * 8)
        with org_context(self.org_a):
            from core.workflow import apply_transition
            apply_transition(ins, 'pass', self.pat)
            idx = RecordIndex.objects.get(record_type='quality.inspection', object_id=ins.pk)
            self.assertEqual((idx.status, idx.ball_in_court_user_id), (Inspection.PASSED, None))
            actions = list(AuditEntry.objects.filter(record_idx=idx).values_list('action', flat=True))
        self.assertEqual(actions, ['created', 'pass'])

    def test_items_belong_to_their_inspection(self):
        ins, _ = self.inspection()
        other, other_items = self.inspection()
        self.login(self.pat)
        r = self.client.post(self.purl('inspection_item', ins.pk, other_items[0].pk), {'result': 'PASS'})
        self.assertEqual(r.status_code, 404)

    def test_removing_a_photo(self):
        ins, items = self.inspection()
        self.login(self.sam)
        self.client.post(self.purl('inspection_item', ins.pk, items[0].pk), {'result': 'PASS', 'photo': jpeg()})
        with org_context(self.org_a):
            photo = QualityPhoto.objects.get()
        self.login(self.alice)  # an owner may remove anyone's
        self.client.post(self.purl('quality_photo_remove', photo.pk))
        with org_context(self.org_a):
            self.assertFalse(QualityPhoto.objects.exists())
        self.assertEqual(InspectionItem, type(self.fresh(items[0])))

    def test_someone_elses_photo_cant_be_removed_by_other_staff(self):
        ins, items = self.inspection()
        self.login(self.pat)
        self.client.post(self.purl('inspection_item', ins.pk, items[0].pk), {'result': 'PASS', 'photo': jpeg()})
        with org_context(self.org_a):
            photo = QualityPhoto.objects.get()
        self.login(self.sam)
        self.assertEqual(self.client.post(self.purl('quality_photo_remove', photo.pk)).status_code, 403)

    def test_team_roles(self):
        """The site manager on the team can use Quality; a member of staff not on the job can't."""
        ins, _ = self.inspection()
        self.login(self.sam)
        self.assertEqual(self.client.get(ins.get_absolute_url()).status_code, 200)
        self.login(self.stan)
        self.assertEqual(self.client.get(ins.get_absolute_url()).status_code, 403)
        with org_context(self.org_a):
            ProjectMember.objects.create(project=self.pa, user=self.stan, role=ProjectMember.ROLE_QS)
        self.assertEqual(self.client.get(ins.get_absolute_url()).status_code, 200)
        self.assertEqual(rules.PASS, 'PASS')
