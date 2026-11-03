"""
Meetings: numbering, the index and audit; planning a meeting from the company agenda with actions
brought forward; writing minutes and actions; issuing them by email with a link (never an
attachment) and correcting them; Needs me for action owners and for the chair; what each role sees
(staff, client, consultant, subcontractor, tendering subcontractor, other staff, another company);
the agenda templates; CSV exports; archived jobs; and failing closed with no organisation.
"""
from datetime import timedelta

from django.core import mail
from django.utils import timezone

import actions
from changes.tests.base import CommercialTestBase
from core.models import AuditEntry, RecordIndex
from projects.models import Project
from tenancy.context import TenantContextMissing, org_context
from tenancy.models import Notification

from .. import services
from ..actions import items
from ..models import CLIENT, PROGRESS, AgendaTemplate, Attendee, Meeting, MeetingAction


class MeetingTestBase(CommercialTestBase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()

    def meeting(self, kind=PROGRESS, days_ago=0, attendees=None, project=None, org=None, actor=None, **kw):
        with org_context(org or self.org_a):
            return services.create(project or self.pa, actor or self.pat, kind=kind,
                                   held_on=self.today - timedelta(days=days_ago),
                                   attendees=attendees if attendees is not None else [self.pat, self.carla], **kw)

    def action(self, meeting, owner=None, due=None, title='Confirm the bar back veneer', **kw):
        with org_context(meeting.organization):
            a = MeetingAction(meeting=meeting, title=title, owner=owner, due_date=due, **kw)
            a.save(actor=meeting.created_by)
            return a

    def staff_items(self, user, prefix):
        """Needs me for a member of staff, through the registry, keeping one kind (record index rows)."""
        with org_context(self.org_a):
            return [i for i in actions.for_user(self.request_for(user)) if i.ref.startswith(prefix)]

    def issue(self, meeting, user=None):
        with org_context(meeting.organization):
            meeting.refresh_from_db()
            return services.issue(meeting, user or self.pat)


class RecordTests(MeetingTestBase):
    def test_numbered_per_project_internal_titled_and_indexed(self):
        a, b = self.meeting(), self.meeting(kind=CLIENT)
        other = self.meeting(project=self.pb, org=self.org_b, actor=self.bob, attendees=[])
        self.assertEqual((a.ref, b.ref, other.ref), ('MTG-001', 'MTG-002', 'MTG-001'))
        self.assertEqual(a.visibility, 'INTERNAL')
        self.assertTrue(a.title.startswith('Site progress meeting '))
        self.assertTrue(b.title.startswith('Client meeting '))
        with org_context(self.org_a):
            self.assertEqual(RecordIndex.objects.filter(record_type='meetings.meeting').count(), 2)
            self.assertTrue(AuditEntry.objects.filter(record_idx__object_id=a.pk, action='created').exists())

    def test_externals_never_see_meetings_through_the_index(self):
        m = self.meeting()
        self.issue(m)
        from core.access import visible_index
        for u in (self.carla, self.sid):
            with org_context(self.org_a):
                self.assertFalse(visible_index(self.request_for(u)).filter(record_type='meetings.meeting').exists())

    def test_agenda_comes_from_the_company_template_and_can_be_changed(self):
        m = self.meeting()
        with org_context(self.org_a):
            headings = list(m.items.values_list('heading', flat=True))
        self.assertEqual(headings[:3], ['Attendance and apologies', 'Previous minutes and actions', 'Health and safety'])
        with org_context(self.org_a):
            t = AgendaTemplate.objects.get(kind=PROGRESS)
            t.items = 'Safety first\nProgress'
            t.save()
        m2 = self.meeting()
        with org_context(self.org_a):
            self.assertEqual(list(m2.items.values_list('heading', flat=True)), ['Safety first', 'Progress'])
            self.assertEqual(m.items.count(), len(headings))  # existing meetings keep theirs
            self.assertEqual(AgendaTemplate.objects.count(), 3)
        with org_context(self.org_b):
            self.assertEqual(AgendaTemplate.objects.count(), 0)  # seeded per company on first use

    def test_open_actions_are_brought_forward_to_the_next_meeting_of_the_same_kind(self):
        first = self.meeting(days_ago=14)
        open_one = self.action(first, owner=self.sid)
        done = self.action(first, title='Order the tiles')
        with org_context(self.org_a):
            services.close_action(done, self.pat, MeetingAction.DONE)
        client_mtg = self.meeting(kind=CLIENT, days_ago=10)
        client_action = self.action(client_mtg, owner=self.carla)
        second = self.meeting(days_ago=7)
        with org_context(self.org_a):
            self.assertEqual(list(second.brought_forward.all()), [open_one])
            self.assertEqual(list(self.meeting(kind=CLIENT).brought_forward.all()), [client_action])
            second_action = self.action(second, owner=self.pat)
            third = self.meeting()
            self.assertEqual(set(third.brought_forward.all()), {open_one, second_action})

    def test_action_numbers_and_references(self):
        m = self.meeting()
        a, b = self.action(m), self.action(m)
        self.assertEqual((a.reference, b.reference), ('ACT-001', 'ACT-002'))
        with org_context(self.org_a):
            self.assertEqual(RecordIndex.objects.filter(record_type='meetings.meetingaction').count(), 2)
        with org_context(self.org_a):
            a.due_date = self.today - timedelta(days=1)
            self.assertEqual((a.status_label, a.status_tone), ('Overdue', 'bad'))


class FlowTests(MeetingTestBase):
    def test_plan_from_the_form_with_only_kind_and_date(self):
        self.login(self.pat)
        r = self.client.get(self.url('meeting_create', self.pa.pk))
        self.assertContains(r, 'Site progress meeting')
        self.assertContains(r, 'Carla Client')
        self.assertNotContains(r, 'Bea Bidder')  # tendering subs can't see the job
        r = self.client.post(self.url('meeting_create', self.pa.pk), {'kind': 'PRESTART', 'held_on': self.today.isoformat()})
        with org_context(self.org_a):
            m = Meeting.objects.get()
        self.assertRedirects(r, m.get_absolute_url())
        self.assertEqual((m.kind, m.chair, m.created_by), ('PRESTART', self.pat, self.pat))
        r = self.client.post(self.url('meeting_create', self.pa.pk), {'kind': 'PROGRESS', 'held_on': '',
                                                                         'starts_at': '9am'})
        self.assertContains(r, 'Enter a time like 09:30.')

    def test_attendees_and_others(self):
        self.login(self.pat)
        self.client.post(self.url('meeting_create', self.pa.pk), {
            'kind': 'PROGRESS', 'held_on': self.today.isoformat(), 'attendees': [self.pat.pk, self.sid.pk],
            'others': 'Building control officer, Leeds City Council\nFire engineer'})
        with org_context(self.org_a):
            m = Meeting.objects.get()
            people = [(a.user, a.name, a.company) for a in m.attendees.all()]
        self.assertIn((self.sid, '', ''), people)
        self.assertIn((None, 'Building control officer', 'Leeds City Council'), people)
        self.assertIn((None, 'Fire engineer', ''), people)
        # Someone not on the job can't be slipped in
        self.client.post(self.url('meeting_create', self.pa.pk), {
            'kind': 'PROGRESS', 'held_on': self.today.isoformat(), 'attendees': [self.bob.pk]})
        with org_context(self.org_a):
            self.assertEqual(Meeting.objects.count(), 1)

    def test_minutes_actions_and_issue_by_email_with_a_link(self):
        m = self.meeting(attendees=[self.pat, self.carla, self.connie])
        self.login(self.pat)
        url = m.get_absolute_url()
        with org_context(self.org_a):
            item = m.items.first()
            connie = m.attendees.get(user=self.connie)
            keep = [a.pk for a in m.attendees.exclude(pk=connie.pk)]
        r = self.client.post(url, {'action': 'minutes', f'item-{item.pk}': 'All present.', 'notes': 'Good meeting',
                                   'present': keep})
        self.assertEqual(r.status_code, 302)
        r = self.client.post(url, {'action': 'add_action', 'act-title': 'Confirm the veneer',
                                   'act-owner': self.carla.pk, 'act-due_date': (self.today + timedelta(days=5)).isoformat()})
        self.assertEqual(r.status_code, 302)
        self.client.post(url, {'action': 'add_action', 'act-title': 'Chase the fire engineer',
                               'act-owner': self.sid.pk})
        with org_context(self.org_a):
            item.refresh_from_db()
            self.assertEqual(item.minutes, 'All present.')
            self.assertFalse(m.attendees.get(user=self.connie).present)
            self.assertEqual(m.actions.count(), 2)
        # Not on Needs me until issued
        with org_context(self.org_a):
            self.assertEqual(items(self.request_for(self.carla)), [])
        mail.outbox.clear()
        r = self.client.post(url, {'action': 'issue'})
        self.assertRedirects(r, url)
        with org_context(self.org_a):
            m.refresh_from_db()
        self.assertEqual((m.status, m.issue_count, m.issued_by), ('ISSUED', 1, self.pat))
        to = sorted(e.to[0] for e in mail.outbox)
        # Carla and Connie (attendees, Connie sent apologies) and Sid (an action, not at the meeting); not Pat (issued it)
        self.assertEqual(to, ['carla@c.test', 'connie@c.test', 'sid@s.test'])
        for e in mail.outbox:
            self.assertEqual(e.attachments, [])
            self.assertIn('http', e.body)
            self.assertTrue(e.subject.endswith(m.title))
        carla_mail = next(e for e in mail.outbox if e.to == ['carla@c.test'])
        self.assertIn('Confirm the veneer', carla_mail.body)
        self.assertIn(m.get_absolute_url(), carla_mail.body)
        sid_mail = next(e for e in mail.outbox if e.to == ['sid@s.test'])
        self.assertIn(self.url('meeting_list', self.pa.pk), sid_mail.body)  # his actions, not the minutes
        with org_context(self.org_a):
            self.assertEqual(Notification.objects.filter(user=self.carla).count(), 1)
        # Locked once issued
        r = self.client.post(url, {'action': 'minutes', f'item-{item.pk}': 'Changed'})
        with org_context(self.org_a):
            item.refresh_from_db()
        self.assertEqual(item.minutes, 'All present.')
        # Correct and re-issue
        self.client.post(url, {'action': 'correct'})
        self.client.post(url, {'action': 'minutes', f'item-{item.pk}': 'All present except Connie.'})
        mail.outbox.clear()
        self.client.post(url, {'action': 'issue'})
        self.assertTrue(all(e.subject.startswith('[Alpha Fitout] Corrected minutes') for e in mail.outbox))
        with org_context(self.org_a):
            m.refresh_from_db()
            self.assertEqual(m.issue_count, 2)
            self.assertEqual(list(AuditEntry.objects.filter(record_idx__object_id=m.pk, action__in=['issue', 'correct'])
                                  .values_list('action', flat=True)), ['issue', 'correct', 'issue'])

    def test_stale_minutes_form_is_refused(self):
        m = self.meeting()
        self.login(self.pat)
        with org_context(self.org_a):
            item = m.items.first()
        r = self.client.post(m.get_absolute_url(), {'action': 'minutes', 'version': m.version + 5,
                                                    f'item-{item.pk}': 'x'}, follow=True)
        self.assertContains(r, 'Someone else saved these minutes')
        with org_context(self.org_a):
            item.refresh_from_db()
        self.assertEqual(item.minutes, '')

    def test_action_errors_say_how_to_fix(self):
        m = self.meeting()
        self.login(self.pat)
        r = self.client.post(m.get_absolute_url(), {'action': 'add_action', 'act-title': ''}, follow=True)
        self.assertContains(r, 'The action wasn&#x27;t added')
        with org_context(self.org_a):
            self.assertFalse(m.actions.exists())

    def test_cancel_and_reinstate(self):
        m = self.meeting()
        self.login(self.pat)
        self.client.post(m.get_absolute_url(), {'action': 'cancel'})
        with org_context(self.org_a):
            m.refresh_from_db()
        self.assertEqual(m.status, 'CANCELLED')
        self.client.post(m.get_absolute_url(), {'action': 'reinstate'})
        with org_context(self.org_a):
            m.refresh_from_db()
        self.assertEqual(m.status, 'PLANNED')

    def test_edit_details_and_people(self):
        m = self.meeting()
        self.login(self.pat)
        r = self.client.post(self.url('meeting_edit', self.pa.pk, m.pk), {
            'title': 'Progress meeting 4', 'held_on': self.today.isoformat(), 'starts_at': '10:00',
            'location': 'Site office', 'chair': self.pat.pk, 'version': m.version,
            'p-attendees': [self.pat.pk, self.sid.pk], 'p-others': 'Fire engineer, Firecheck Ltd'})
        self.assertRedirects(r, m.get_absolute_url())
        with org_context(self.org_a):
            m.refresh_from_db()
            self.assertEqual((m.title, m.starts_at, m.location), ('Progress meeting 4', '10:00', 'Site office'))
            self.assertEqual({a.user_id for a in m.attendees.filter(user__isnull=False)}, {self.pat.pk, self.sid.pk})
            self.assertEqual(m.attendees.get(user__isnull=True).company, 'Firecheck Ltd')


class NeedsMeTests(MeetingTestBase):
    def test_owners_get_their_actions_once_issued_staff_and_externals(self):
        m = self.meeting()
        a_sid = self.action(m, owner=self.sid, due=self.today + timedelta(days=2))
        a_pat = self.action(m, owner=self.pat, due=self.today - timedelta(days=1))
        self.assertEqual(self.staff_items(self.pat, 'ACT-'), [])  # not until the minutes are issued
        self.issue(m, self.alice)
        with org_context(self.org_a):
            a_sid.refresh_from_db()
            a_pat.refresh_from_db()
        self.assertEqual((a_sid.ball_in_court_user, a_pat.ball_in_court_user), (self.sid, self.pat))
        with org_context(self.org_a):
            sid_items = items(self.request_for(self.sid))
        self.assertEqual([i.ref for i in sid_items], [a_sid.reference])
        self.assertEqual(sid_items[0].due, a_sid.due_date)
        self.assertIn(self.url('meeting_list', self.pa.pk), sid_items[0].url)
        # Staff: through the record index (no query of our own behind the badge), worded for actions
        pat_items = self.staff_items(self.pat, 'ACT-')
        self.assertEqual([i.ref for i in pat_items], [a_pat.reference])
        self.assertEqual((pat_items[0].action, pat_items[0].kind_label), ('Do it, then mark it done', 'Meeting actions'))
        self.assertTrue(pat_items[0].is_overdue)
        self.assertIn(m.get_absolute_url(), pat_items[0].url)
        with org_context(self.org_a):
            self.assertEqual(items(self.request_for(self.pat)), [])  # the hook is for externals only
        # Through the registry (portal_projects check) and on the Needs me page
        self.login(self.sid)
        r = self.client.get(self.url('needs_me'))
        self.assertContains(r, 'Confirm the bar back veneer')
        with org_context(self.org_a):
            services.close_action(a_sid, self.sid, MeetingAction.DONE, 'Sent over')
            self.assertEqual((a_sid.status, a_sid.update, a_sid.closed_by), ('DONE', 'Sent over', self.sid))
            self.assertEqual(items(self.request_for(self.sid)), [])
            services.close_action(a_pat, self.pat, MeetingAction.DONE)
        self.assertEqual(self.staff_items(self.pat, 'ACT-'), [])
        with org_context(self.org_a):
            services.close_action(a_pat, self.pat, MeetingAction.OPEN)  # reopened: back with the owner
        self.assertEqual([i.ref for i in self.staff_items(self.pat, 'ACT-')], [a_pat.reference])

    def test_externals_cannot_drop_or_reopen(self):
        m = self.meeting()
        a = self.action(m, owner=self.sid)
        self.issue(m)
        from core.exceptions import TransitionForbidden
        with org_context(self.org_a):
            a.refresh_from_db()
        with org_context(self.org_a), self.assertRaises(TransitionForbidden):
            services.close_action(a, self.sid, MeetingAction.DROPPED)

    def test_chair_has_the_minutes_to_issue_until_they_are(self):
        old = self.meeting(days_ago=3)
        self.meeting(kind=CLIENT, actor=self.alice)  # chaired by someone else
        got = self.staff_items(self.pat, 'MTG-')
        self.assertEqual([i.ref for i in got], [old.ref])
        self.assertEqual((got[0].due, got[0].action, got[0].kind_label),
                         (self.today - timedelta(days=1), 'Finish the minutes and issue them', 'Minutes to issue'))
        self.assertTrue(got[0].is_overdue)
        self.issue(old)
        self.assertEqual(self.staff_items(self.pat, 'MTG-'), [])
        self.login(self.pat)
        self.client.post(old.get_absolute_url(), {'action': 'correct'})
        self.assertEqual([i.ref for i in self.staff_items(self.pat, 'MTG-')], [old.ref])  # whoever corrects it
        with org_context(self.org_a):
            self.assertEqual(items(self.request_for(self.carla)), [])

    def test_archived_jobs_drop_out_and_other_companies_never_show(self):
        m = self.meeting()
        self.action(m, owner=self.sid, due=self.today)
        self.issue(m)
        mb = self.meeting(project=self.pb, org=self.org_b, actor=self.bob, attendees=[])
        self.action(mb, owner=self.sid)  # Sid isn't in Bravo: nothing may leak across
        self.issue(mb, self.bob)
        with org_context(self.org_a):
            self.assertEqual(len(actions.for_user(self.request_for(self.sid), kind='meeting_action')), 1)
            self.pa.archive(self.alice)
            self.assertEqual(items(self.request_for(self.sid)), [])
        self.assertEqual(self.staff_items(self.pat, 'MTG-'), [])


class AccessTests(MeetingTestBase):
    def test_register_and_create_per_role(self):
        self.meeting()
        listing, create = self.url('meeting_list', self.pa.pk), self.url('meeting_create', self.pa.pk)
        for name, want_list, want_create in [
                ('alice', 200, 200), ('adam', 200, 200), ('pat', 200, 200), ('eve', 404, 404),
                ('carla', 200, 403), ('connie', 200, 403), ('sid', 200, 403), ('bea', 403, 403),
                ('nora', 404, 404), ('bob', 404, 404)]:
            with self.subTest(name):
                self.assertEqual(self.status_for(getattr(self, name), listing), want_list)
                self.assertEqual(self.status_for(getattr(self, name), create), want_create)

    def test_externals_see_only_issued_minutes_they_were_listed_at(self):
        m = self.meeting(attendees=[self.pat, self.carla])
        detail = m.get_absolute_url()
        self.assertEqual(self.status_for(self.carla, detail), 404)  # not issued yet
        self.issue(m)
        self.assertEqual(self.status_for(self.carla, detail), 200)
        for u in (self.connie, self.sid):  # not listed
            self.assertEqual(self.status_for(u, detail), 404)
        self.login(self.carla)
        r = self.client.get(detail)
        self.assertNotContains(r, 'Issue the minutes')
        self.assertNotContains(r, 'name="action" value="minutes"')
        self.assertEqual(self.client.post(detail, {'action': 'correct'}).status_code, 403)
        r = self.client.get(self.url('meeting_list', self.pa.pk))
        self.assertContains(r, m.title)

    def test_external_tab_only_when_there_is_something(self):
        self.login(self.sid)
        decisions = self.url('project_decisions', self.pa.pk)  # externals: under "Waiting on you"
        overview = self.url('project_detail', self.pa.pk)
        self.assertNotContains(self.client.get(decisions), self.url('meeting_list', self.pa.pk))
        m = self.meeting(attendees=[self.pat])
        self.action(m, owner=self.sid)
        self.issue(m)
        self.assertContains(self.client.get(decisions), self.url('meeting_list', self.pa.pk))
        self.client.logout()
        self.login(self.pat)
        self.assertContains(self.client.get(overview), self.url('meeting_list', self.pa.pk))  # staff: under More

    def test_owners_update_their_own_actions_only(self):
        m = self.meeting(attendees=[self.pat])
        mine = self.action(m, owner=self.sid)
        theirs = self.action(m, owner=self.carla)
        upd = lambda a: self.url('meeting_action_update', self.pa.pk, a.pk)  # noqa: E731
        self.login(self.sid)
        self.assertEqual(self.client.post(upd(mine), {'status': 'DONE'}).status_code, 404)  # not issued yet
        self.issue(m)
        r = self.client.post(upd(mine), {'status': 'DONE', 'update': 'Fitted Tuesday'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.post(upd(theirs), {'status': 'DONE'}).status_code, 404)
        with org_context(self.org_a):
            mine.refresh_from_db()
            theirs.refresh_from_db()
        self.assertEqual((mine.status, mine.update, mine.closed_by), ('DONE', 'Fitted Tuesday', self.sid))
        self.assertEqual(theirs.status, 'OPEN')
        # Only the site team reopens; an off-site "next" is ignored
        self.assertEqual(self.client.post(upd(mine), {'status': 'OPEN'}).status_code, 403)
        r = self.client.post(upd(mine), {'status': 'DONE', 'update': 'Fitted Tuesday, signed off', 'next': 'https://evil.test/'})
        self.assertTrue(r.url.startswith(self.url('meeting_list', self.pa.pk)))
        with org_context(self.org_a):
            mine.refresh_from_db()
        self.assertEqual(mine.update, 'Fitted Tuesday, signed off')

    def test_another_company_gets_404(self):
        m = self.meeting()
        a = self.action(m, owner=self.pat)
        self.login(self.bob)
        for url in (m.get_absolute_url(), self.url('meeting_edit', self.pa.pk, m.pk),
                    self.url('meeting_actions', self.pa.pk)):
            self.assertEqual(self.client.get(url).status_code, 404)
        # Bravo's own job, Alpha's ids
        self.assertEqual(self.client.get(self.url('meeting_detail', self.pb.pk, m.pk, org=self.org_b)).status_code, 404)
        self.assertEqual(self.client.post(self.url('meeting_action_update', self.pb.pk, a.pk, org=self.org_b),
                                          {'status': 'DONE'}).status_code, 404)

    def test_no_organisation_fails_closed(self):
        self.meeting()
        for model in (Meeting, MeetingAction, Attendee, AgendaTemplate):
            with self.subTest(model.__name__), self.assertRaises(TenantContextMissing):
                list(model.objects.all())

    def test_archived_job_is_read_only(self):
        m = self.meeting()
        with org_context(self.org_a):
            self.pa.archive(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.get(m.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.post(m.get_absolute_url(), {'action': 'issue'}).status_code, 403)
        self.assertEqual(self.client.post(self.url('meeting_create', self.pa.pk),
                                          {'kind': 'PROGRESS', 'held_on': self.today.isoformat()}).status_code, 403)

    def test_meetings_are_for_won_and_live_jobs(self):
        with org_context(self.org_a):
            bid = Project.objects.create(name='A bid', stage=Project.STAGE_LEAD)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.url('meeting_list', bid.pk)).status_code, 403)

    def test_agenda_settings_admins_only(self):
        page = self.url('meeting_agendas')
        self.assertEqual(self.status_for(self.alice, page), 200)
        self.assertEqual(self.status_for(self.adam, page), 200)
        self.assertNotEqual(self.status_for(self.pat, page), 200)
        self.assertNotEqual(self.status_for(self.carla, page), 200)
        self.login(self.adam)
        r = self.client.post(page, {'kind': 'PRESTART', 'prestart-name': 'Subcontractor pre-start',
                                    'prestart-items': 'Welcome\n\n  Scope  \n'})
        self.assertRedirects(r, page)
        with org_context(self.org_a):
            t = AgendaTemplate.objects.get(kind='PRESTART')
        self.assertEqual((t.name, t.item_list), ('Subcontractor pre-start', ['Welcome', 'Scope']))
        r = self.client.post(page, {'kind': 'PRESTART', 'prestart-name': 'X', 'prestart-items': '  '})
        self.assertContains(r, 'Add at least one agenda item')


class ExportTests(MeetingTestBase):
    def test_csv_exports_are_formula_safe(self):
        m = self.meeting(location='=cmd|calc')
        self.action(m, title='  =HYPERLINK("http://x")', owner=self.pat)
        self.login(self.pat)
        r = self.client.get(self.url('meeting_list', self.pa.pk) + '?export=csv')
        self.assertEqual(r['Content-Type'], 'text/csv')
        self.assertIn("'=cmd|calc", r.content.decode())
        r = self.client.get(self.url('meeting_actions', self.pa.pk) + '?export=csv')
        body = r.content.decode()
        self.assertTrue(body.startswith('Ref,Action,Who,Due,Status'))
        self.assertIn("'  =HYPERLINK", body)
        self.client.logout()
        self.login(self.carla)
        r = self.client.get(self.url('meeting_actions', self.pa.pk) + '?export=csv')
        self.assertNotEqual(r.get('Content-Type'), 'text/csv')

    def test_register_filters_search_and_empty_states(self):
        self.login(self.pat)
        r = self.client.get(self.url('meeting_list', self.pa.pk))
        self.assertContains(r, 'Plan the first meeting')
        self.meeting()
        self.meeting(kind=CLIENT)
        r = self.client.get(self.url('meeting_list', self.pa.pk) + '?kind=CLIENT')
        self.assertContains(r, 'Client meeting')
        self.assertNotContains(r, 'MTG-001')
        r = self.client.get(self.url('meeting_list', self.pa.pk) + '?q=nothing-like-this')
        self.assertContains(r, 'No meetings match')
        r = self.client.get(self.url('meeting_actions', self.pa.pk))
        self.assertContains(r, 'No open actions')
