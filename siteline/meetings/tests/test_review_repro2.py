"""
Review repro (stream I, wave 2): marking an agenda heading internal only while an open action under it
belongs to someone outside the company. The add-action form already refuses that combination, but ticking
"Internal only" on the minutes afterwards made the same state: the action vanished from its owner's
portal, Needs me and email, and (its ball being with them) from every staff Needs me too, so nobody
was ever asked to do it.
"""
from tenancy.context import org_context

from ..actions import items
from ..models import PROGRESS, AgendaItem, AgendaTemplate, Meeting
from .test_meetings import MeetingTestBase


class InternalFlagWithExternalActionTests(MeetingTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            t = AgendaTemplate.for_org(self.org_a)[PROGRESS]
            t.items = 'Health and safety\nProgramme'
            t.save()
        self.m = self.meeting(attendees=[self.pat, self.carla, self.sid])
        with org_context(self.org_a):
            self.hs, self.prog = list(self.m.items.all())
        self.subs = self.action(self.m, owner=self.sid, title='Send the revised RAMS', item=self.hs)

    def post_minutes(self, internal):
        with org_context(self.org_a):
            version = Meeting.objects.get(pk=self.m.pk).version
        self.login(self.pat)
        return self.client.post(self.m.get_absolute_url(), {
            'action': 'minutes', 'version': version, 'internal_listed': '1',
            'internal': [str(i.pk) for i in internal], f'item-{self.hs.pk}': 'Hoarding moved'}, follow=True)

    def flags(self):
        with org_context(self.org_a):
            return dict(AgendaItem.objects.filter(meeting=self.m).values_list('pk', 'internal_only'))

    def test_a_heading_with_an_external_owners_open_action_stays_shared(self):
        r = self.post_minutes([self.hs, self.prog])
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Health and safety')
        flags = self.flags()
        self.assertFalse(flags[self.hs.pk])   # refused: Sid's action would vanish
        self.assertTrue(flags[self.prog.pk])  # the other heading is still marked
        with org_context(self.org_a):
            self.assertEqual(AgendaItem.objects.get(pk=self.hs.pk).minutes, 'Hoarding moved')  # minutes kept
        self.issue(self.m)
        with org_context(self.org_a):
            refs = [i.ref for i in items(self.request_for(self.sid))]
        self.assertIn(self.subs.reference, refs)  # still on Sid's Needs me

    def test_allowed_once_the_action_is_with_our_team_or_closed(self):
        with org_context(self.org_a):
            type(self.subs).objects.filter(pk=self.subs.pk).update(owner=self.alice)
        self.post_minutes([self.hs])
        self.assertTrue(self.flags()[self.hs.pk])
