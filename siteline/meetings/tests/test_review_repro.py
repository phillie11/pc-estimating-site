"""
Review repros for meetings (branch claude/r3-programme-review). Each failed on origin/claude/r3-programme.
"""
from django.core import mail

from projects.models import ProjectMember
from tenancy.context import org_context

from ..models import Meeting
from .test_meetings import MeetingTestBase


class BroughtForwardLeakRepro(MeetingTestBase):
    def test_external_does_not_see_actions_from_minutes_never_issued(self):
        # An internal meeting minuted but never issued; its open actions are carried into the next
        # meeting of the same kind, whose issued minutes the client can open.
        draft = self.meeting(days_ago=7, attendees=[self.pat])
        self.action(draft, owner=self.pat, title='Chase the client about the late payment')
        issued_before = self.meeting(days_ago=5, attendees=[self.pat, self.carla])
        self.action(issued_before, owner=self.pat, title='Send the door schedule')
        self.issue(issued_before)
        m = self.meeting(attendees=[self.pat, self.carla])
        self.issue(m)
        self.login(self.carla)
        r = self.client.get(self.url('meeting_detail', self.pa.pk, m.pk))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Send the door schedule')  # from issued minutes: fine
        self.assertNotContains(r, 'late payment')
        # Staff still see everything brought forward
        self.client.logout()
        self.login(self.pat)
        self.assertContains(self.client.get(self.url('meeting_detail', self.pa.pk, m.pk)), 'late payment')


class EditIssuedRepro(MeetingTestBase):
    def test_issued_minutes_cannot_be_edited_without_correcting_them(self):
        m = self.meeting()
        self.issue(m)
        self.login(self.pat)
        url = self.url('meeting_edit', self.pa.pk, m.pk)
        r = self.client.get(url)
        self.assertRedirects(r, self.url('meeting_detail', self.pa.pk, m.pk))
        r = self.client.post(url, {'title': 'Rewritten after issue', 'held_on': m.held_on.isoformat(),
                                   'starts_at': '', 'location': '', 'chair': self.pat.pk,
                                   'version': m.version, 'p-attendees': [self.pat.pk, self.connie.pk]})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            m.refresh_from_db()
            self.assertNotEqual(m.title, 'Rewritten after issue')
            self.assertFalse(m.attendees.filter(user=self.connie).exists())


class RecipientsRepro(MeetingTestBase):
    def test_people_taken_off_the_project_are_not_emailed(self):
        m = self.meeting(attendees=[self.pat, self.carla, self.sid])
        self.action(m, owner=self.connie, title='Confirm the fire strategy')
        with org_context(self.org_a):
            ProjectMember.objects.filter(project=self.pa, user__in=[self.sid, self.connie]).delete()
        mail.outbox.clear()
        self.issue(m)
        to = {addr for msg in mail.outbox for addr in msg.to}
        self.assertIn(self.carla.email, to)
        self.assertNotIn(self.sid.email, to)
        self.assertNotIn(self.connie.email, to)
        with org_context(self.org_a):
            self.assertEqual(Meeting.objects.get(pk=m.pk).status, Meeting.STATUS_ISSUED)
