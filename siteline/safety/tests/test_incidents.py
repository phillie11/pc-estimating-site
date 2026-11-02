"""
Incidents and near misses: the phone report, numbering and the record index, the investigate /
close / reopen workflow and who may do it, what stops closing, RIDDOR, actions and photos (and
their Needs me items), the register's filters and CSV, the company-wide register, and isolation.
"""
from datetime import timedelta

from django.core import mail
from django.utils import timezone

import actions
from core.models import AuditEntry, RecordIndex
from tenancy.context import TenantContextMissing, org_context

from .. import rules
from ..actions import items
from ..models import Incident, IncidentAction, IncidentPhoto
from .base import SafetyTestBase, jpeg


class ReportTests(SafetyTestBase):
    def report(self, **extra):
        data = {'description': 'Ladder slipped on the wet floor.\nNobody hurt.', 'location': 'Stair core',
                'anyone_hurt': 'False', 'occurred_at': timezone.localtime().strftime('%Y-%m-%dT%H:%M'), **extra}
        return self.client.post(self.u('incident_report', self.pa.pk), data)

    def test_site_team_reports_in_one_post_and_the_pm_is_told(self):
        self.login(self.sam)
        r = self.report(photo=jpeg())
        with org_context(self.org_a):
            i = Incident.objects.get(project=self.pa)
            self.assertRedirects(r, i.get_absolute_url(), fetch_redirect_response=False)
            self.assertEqual((i.ref, i.title, i.kind, i.status), ('INC-001', 'Ladder slipped on the wet floor.',
                                                                  Incident.KIND_NEAR_MISS, Incident.STATUS_REPORTED))
            self.assertEqual(i.ball_in_court_user, self.pat)
            self.assertEqual(i.due_date, rules.investigation_due(timezone.localdate()))
            self.assertEqual(i.visibility, 'INTERNAL')
            self.assertEqual(i.created_by, self.sam)
            photo = IncidentPhoto.objects.get(incident=i)
            self.assertTrue(photo.thumbnail and photo.display)
            self.assertTrue(RecordIndex.objects.filter(record_type='safety.incident', object_id=i.pk, is_open=True,
                                                       ball_in_court_user=self.pat).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Near miss reported: INC-001', mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, ['pat@a.test'])

    def test_only_what_where_needed(self):
        self.login(self.sam)
        r = self.client.post(self.u('incident_report', self.pa.pk), {'anyone_hurt': 'False'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Say what happened')  # the only two required fields, with errors that say what to do
        self.assertContains(r, 'Say where it happened')
        r = self.client.post(self.u('incident_report', self.pa.pk), {'description': 'Trip', 'location': 'Yard'})
        self.assertEqual(r.status_code, 302)  # "when" defaults to now, "anyone hurt" to no
        with org_context(self.org_a):
            i = Incident.objects.get()
            self.assertFalse(i.anyone_hurt)
            self.assertLess(abs((timezone.now() - i.occurred_at).total_seconds()), 60)

    def test_someone_hurt_is_an_injury(self):
        self.login(self.pat)
        self.report(anyone_hurt='True')
        with org_context(self.org_a):
            self.assertEqual(Incident.objects.get().kind, Incident.KIND_INJURY)
        self.assertEqual(mail.outbox, [])  # the PM reported it themselves: nobody to tell

    def test_future_time_refused_and_not_a_photo_refused(self):
        self.login(self.sam)
        later = (timezone.localtime() + timedelta(hours=3)).strftime('%Y-%m-%dT%H:%M')
        self.assertContains(self.report(occurred_at=later), 'in the future')
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = self.report(photo=SimpleUploadedFile('notes.jpg', b'not a picture', content_type='image/jpeg'))
        self.assertEqual(r.status_code, 200)
        with org_context(self.org_a):
            self.assertFalse(Incident.objects.exists())

    def test_phone_form_markup(self):
        self.login(self.sam)
        r = self.client.get(self.u('incident_report', self.pa.pk))
        self.assertContains(r, 'accept="image/*"')
        self.assertContains(r, 'capture="environment"')
        self.assertContains(r, 'type="datetime-local"')
        self.assertContains(r, 'class="site-mode touch"')

    def test_numbering_is_per_job(self):
        a = self.incident()
        b = self.incident()
        other = self.incident(project=self.pb, org=self.org_b, actor=self.bob)
        self.assertEqual((a.ref, b.ref, other.ref), ('INC-001', 'INC-002', 'INC-001'))

    def test_externals_never_see_incidents_through_the_index(self):
        self.incident()
        from core.access import visible_index
        for u in (self.carla, self.connie, self.sid):
            with org_context(self.org_a):
                self.assertFalse(visible_index(self.request_for(u)).filter(record_type='safety.incident').exists())


class WorkflowTests(SafetyTestBase):
    def post(self, user, incident, transition, **extra):
        self.login(user)
        return self.client.post(self.u('incident_transition', self.pa.pk, incident.pk),
                                {'transition': transition, **extra})

    def reload(self, i):
        with org_context(self.org_a):
            return Incident.objects.get(pk=i.pk)

    def test_only_pm_and_admins_investigate_and_close(self):
        i = self.incident()
        self.assertEqual(self.post(self.sam, i, 'investigate').status_code, 403)
        self.assertEqual(self.reload(i).status, Incident.STATUS_REPORTED)
        self.post(self.pat, i, 'investigate')
        i = self.reload(i)
        self.assertEqual((i.status, i.ball_in_court_user), (Incident.STATUS_INVESTIGATING, self.pat))
        self.assertEqual(self.post(self.sam, i, 'close').status_code, 403)
        for who in (self.alice, self.adam):
            with self.subTest(who=who.email):
                self.assertEqual(self.post(who, i, 'investigate').status_code, 302)  # wrong status: a message
                self.assertEqual(self.reload(i).status, Incident.STATUS_INVESTIGATING)

    def test_closing_needs_findings_actions_done_and_riddor_reported(self):
        i = self.incident(riddor_category=rules.RIDDOR_OVER_7)
        with org_context(self.org_a):
            a = IncidentAction.objects.create(incident=i, description='Toolbox talk', owner=self.pat)
        r = self.post(self.pat, i, 'close')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.reload(i).status, Incident.STATUS_REPORTED)
        page = self.client.get(i.get_absolute_url())
        for words in ('Write up the findings', 'RIDDOR', 'Finish the open actions'):
            self.assertContains(page, words)
        with org_context(self.org_a):
            i = Incident.objects.get(pk=i.pk)
            i.findings, i.riddor_reported_on = 'Lift gate latch was loose.', timezone.localdate()
            i.save(actor=self.pat)
        self.client.post(self.u('incident_action_update', self.pa.pk, i.pk, a.pk), {'do': 'done'})
        self.post(self.pat, i, 'close')
        i = self.reload(i)
        self.assertEqual(i.status, Incident.STATUS_CLOSED)
        self.assertIsNone(i.ball_in_court_user)
        self.assertEqual(i.closed_by, self.pat)
        with org_context(self.org_a):
            self.assertFalse(RecordIndex.objects.get(record_type='safety.incident', object_id=i.pk).is_open)
        # Closed: nothing changes until it is reopened, with a reason
        self.assertRedirects(self.client.post(self.u('incident_edit', self.pa.pk, i.pk), {'description': 'x'}),
                             i.get_absolute_url(), fetch_redirect_response=False)
        self.post(self.pat, i, 'reopen')
        self.assertEqual(self.reload(i).status, Incident.STATUS_CLOSED)
        self.post(self.pat, i, 'reopen', note='New information from the hire company')
        i = self.reload(i)
        self.assertEqual((i.status, i.closed_at), (Incident.STATUS_INVESTIGATING, None))
        with org_context(self.org_a):
            notes = list(AuditEntry.objects.filter(record_idx__object_id=i.pk,
                                                   record_idx__record_type='safety.incident')
                         .values_list('action', 'note'))
        self.assertIn(('reopen', 'New information from the hire company'), notes)
        self.assertIn(('action done', 'Toolbox talk'), notes)

    def test_unknown_transition_is_404(self):
        i = self.incident()
        self.assertEqual(self.post(self.pat, i, 'purge').status_code, 404)

    def test_edit_is_audited_and_refuses_a_stale_form(self):
        i = self.incident()
        self.login(self.sam)
        data = {'kind': 'INJURY', 'description': 'Cut hand', 'location': 'Bar', 'anyone_hurt': 'on',
                'occurred_at': timezone.localtime(i.occurred_at).strftime('%Y-%m-%dT%H:%M'), 'injured_is_worker': 'on',
                'days_off': '9', 'riddor_category': '', 'version': i.version}
        r = self.client.get(self.u('incident_edit', self.pa.pk, i.pk))
        self.assertContains(r, 'Over')  # no category yet: nothing suggested until days off are known
        self.assertEqual(self.client.post(self.u('incident_edit', self.pa.pk, i.pk), data).status_code, 302)
        r = self.client.post(self.u('incident_edit', self.pa.pk, i.pk), data)  # same version again: stale
        self.assertContains(r, 'Someone else changed this')
        page = self.client.get(i.get_absolute_url())
        self.assertContains(page, 'Worker off work or on light duties for more than 7 days')  # the suggestion
        with org_context(self.org_a):
            self.assertTrue(AuditEntry.objects.filter(record_idx__object_id=i.pk, action='updated').exists())

    def test_riddor_date_needs_a_reportable_category(self):
        i = self.incident()
        self.login(self.pat)
        r = self.client.post(self.u('incident_edit', self.pa.pk, i.pk), {
            'kind': 'NEAR_MISS', 'description': 'x', 'location': 'y', 'version': i.version,
            'occurred_at': timezone.localtime(i.occurred_at).strftime('%Y-%m-%dT%H:%M'),
            'riddor_reported_on': timezone.localdate().isoformat()})
        self.assertContains(r, 'Pick the RIDDOR category')


class RiddorAndActionsNeedsMeTests(SafetyTestBase):
    def test_riddor_due_goes_to_the_pm_with_the_deadline(self):
        when = timezone.now() - timedelta(days=3)
        i = self.incident(riddor_category=rules.RIDDOR_SPECIFIED, occurred_at=when, anyone_hurt=True)
        with org_context(self.org_a):
            mine = [x for x in items(self.request_for(self.pat)) if x.key == f'safety:riddor:{i.pk}']
            self.assertEqual(len(mine), 1)
            self.assertEqual(mine[0].due, timezone.localdate(when) + timedelta(days=10))
            self.assertEqual(items(self.request_for(self.sam)), [])
            i.riddor_reported_on, i.riddor_reference = timezone.localdate(), 'HSE-123'
            i.save(actor=self.pat)
            self.assertFalse([x for x in items(self.request_for(self.pat)) if x.key.startswith('safety:riddor')])

    def test_open_incident_is_on_the_pms_needs_me_through_the_index(self):
        i = self.incident()
        self.login(self.pat)
        r = self.client.get(self.u('needs_me'))
        self.assertContains(r, i.title)

    def test_action_owner_gets_it_on_needs_me_and_an_email(self):
        i = self.incident()
        self.login(self.pat)
        due = timezone.localdate() + timedelta(days=2)
        r = self.client.post(self.u('incident_action_add', self.pa.pk, i.pk),
                             {'description': 'Re-brief joiners on bandsaw guards', 'owner': self.sam.pk,
                              'due_date': due.isoformat()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(mail.outbox[-1].to, ['sam@a.test'])
        with org_context(self.org_a):
            a = IncidentAction.objects.get()
            got = [x for x in actions.for_user(self.request_for(self.sam)) if x.kind == 'safety']
            self.assertEqual([(x.title, x.due) for x in got], [('Re-brief joiners on bandsaw guards', due)])
        # Done: off the list
        self.login(self.sam)
        self.client.post(self.u('incident_action_update', self.pa.pk, i.pk, a.pk), {'do': 'done'})
        with org_context(self.org_a):
            self.assertFalse([x for x in items(self.request_for(self.sam)) if x.kind == 'safety'])

    def test_action_owners_are_people_on_the_job(self):
        i = self.incident()
        self.login(self.pat)
        r = self.client.post(self.u('incident_action_add', self.pa.pk, i.pk),
                             {'description': 'x', 'owner': self.stan.pk})
        self.assertContains(r, 'Select a valid choice')
        r = self.client.post(self.u('incident_action_add', self.pa.pk, i.pk),
                             {'description': 'x', 'owner': self.sid.pk})
        self.assertContains(r, 'Select a valid choice')

    def test_only_the_creator_or_pm_removes_an_open_action(self):
        i = self.incident()
        with org_context(self.org_a):
            a = IncidentAction.objects.create(incident=i, description='Fit a guard rail', created_by=self.pat)
        self.login(self.sam)
        self.assertEqual(self.client.post(self.u('incident_action_update', self.pa.pk, i.pk, a.pk),
                                          {'do': 'remove'}).status_code, 403)
        self.login(self.pat)
        self.client.post(self.u('incident_action_update', self.pa.pk, i.pk, a.pk), {'do': 'remove'})
        with org_context(self.org_a):
            self.assertFalse(IncidentAction.objects.exists())

    def test_needs_me_fails_closed_and_is_staff_only(self):
        self.incident(riddor_category=rules.RIDDOR_DEATH)
        with self.assertRaises(TenantContextMissing):
            items(self.request_for(self.pat))
        with org_context(self.org_a):
            for u in (self.carla, self.sid):
                self.assertEqual(items(self.request_for(u)), [])
        with org_context(self.org_b):
            self.assertEqual(items(self.request_for(self.bob, org=self.org_b)), [])


class PhotoTests(SafetyTestBase):
    def test_add_and_serve_photos_staff_on_job_only(self):
        i = self.incident()
        self.login(self.sam)
        r = self.client.post(self.u('incident_photo_add', self.pa.pk, i.pk), {'photo': jpeg(), 'caption': 'Trolley'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            p = IncidentPhoto.objects.get()
        for size in ('thumb', 'display', 'original'):
            self.assertEqual(self.client.get(self.u('incident_photo', self.pa.pk, i.pk, p.pk, size)).status_code, 200)
        self.assertEqual(self.client.get(self.u('incident_photo', self.pa.pk, i.pk, p.pk, 'huge')).status_code, 404)
        for user, code in ((self.stan, 403), (self.carla, 403), (self.sid, 403), (self.bob, 404)):
            self.assertEqual(self.status_for(user, self.u('incident_photo', self.pa.pk, i.pk, p.pk, 'thumb')), code)

    def test_photo_page_uses_the_camera(self):
        i = self.incident()
        self.login(self.sam)
        self.assertContains(self.client.get(self.u('incident_photo_add', self.pa.pk, i.pk)), 'capture="environment"')


class RegisterTests(SafetyTestBase):
    def test_filters_paging_and_csv(self):
        self.incident(description='Near miss at the hoist', location='Hoist')
        hurt = self.incident(description='=HYPERLINK("http://x") cut finger', location='Bar', anyone_hurt=True,
                             kind=Incident.KIND_INJURY, riddor_category=rules.RIDDOR_OVER_7)
        self.login(self.pat)
        url = self.u('incident_list', self.pa.pk)
        r = self.client.get(url + '?riddor=1')
        self.assertContains(r, hurt.ref)
        self.assertNotContains(r, 'Near miss at the hoist')
        r = self.client.get(url + '?q=hoist')
        self.assertContains(r, 'Near miss at the hoist')
        self.assertNotContains(r, hurt.ref + '<')
        self.assertContains(self.client.get(url + '?q=nothing-like-this'), 'No incidents match')
        r = self.client.get(url + '?export=csv')
        self.assertEqual(r['Content-Type'], 'text/csv')
        body = r.content.decode()
        self.assertIn("'=HYPERLINK", body)  # spreadsheet-safe
        self.assertIn('Worker off work or on light duties for more than 7 days', body)

    def test_empty_register_says_what_to_do(self):
        self.login(self.sam)
        r = self.client.get(self.u('incident_list', self.pa.pk))
        self.assertContains(r, 'No incidents or near misses on this job')
        self.assertContains(r, 'Report an incident</a>')

    def test_paged_at_fifty(self):
        with org_context(self.org_a):
            for n in range(52):
                Incident(project=self.pa, description=f'Near miss {n}', location='Yard').save(actor=self.pat)
        self.login(self.pat)
        r = self.client.get(self.u('incident_list', self.pa.pk))
        self.assertEqual(len(r.context['incidents']), 50)
        self.assertContains(r, 'Page 1 of 2')


class CompanyRegisterTests(SafetyTestBase):
    def test_owners_and_admins_only(self):
        self.incident()
        self.incident(project=self.pb, org=self.org_b, actor=self.bob, description='Bravo only')
        url = self.u('company_incidents')
        for user, code in ((self.alice, 200), (self.adam, 200), (self.pat, 403), (self.sam, 403), (self.eve, 403),
                           (self.carla, 403), (self.sid, 403), (self.bob, 404)):
            with self.subTest(user=user.email):
                self.assertEqual(self.status_for(user, url), code)
        self.login(self.alice)
        r = self.client.get(url)
        self.assertContains(r, 'Alpha job')
        self.assertNotContains(r, 'Bravo only')
        csv_ = self.client.get(url + '?export=csv').content.decode()
        self.assertIn('Alpha job', csv_)
        self.assertNotIn('Bravo only', csv_)

    def test_archived_jobs_still_count(self):
        self.incident()
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        r = self.client.get(self.u('company_incidents'))
        self.assertContains(r, 'Board fell off trolley')
