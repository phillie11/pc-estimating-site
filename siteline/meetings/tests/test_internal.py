"""
Release 3 wave 2 meeting defaults (pending owner decision):

  - an agenda heading can be marked internal only. The heading, its minutes and the actions under it
    are kept from clients, consultants and subcontractors on the minutes page, their actions list,
    their Needs me and the minutes email (meetings aren't in global search at all). Agenda templates
    carry it as an "Internal:" prefix.
  - the minutes are due a company-set number of days after the meeting (default 2).
"""
from datetime import timedelta

from django.core import mail

from tenancy.context import TenantContextMissing, org_context

from ..actions import items
from ..models import PROGRESS, AgendaItem, AgendaTemplate, Meeting, MeetingSettings, parse_agenda_line
from .test_meetings import MeetingTestBase

SECRET = 'Margin on the joinery package is thin'


class InternalHeadingTests(MeetingTestBase):
    def setUp(self):
        super().setUp()
        with org_context(self.org_a):
            t = AgendaTemplate.for_org(self.org_a)[PROGRESS]
            t.items = 'Health and safety\nInternal: Commercial and costs\nProgramme'
            t.save()
        self.m = self.meeting(attendees=[self.pat, self.carla, self.sid])
        with org_context(self.org_a):
            self.shared, self.internal, _ = list(self.m.items.all())
            AgendaItem.objects.filter(pk=self.internal.pk).update(minutes=SECRET)
            AgendaItem.objects.filter(pk=self.shared.pk).update(minutes='Hoarding to be moved Friday')
        # An action under the internal heading for our own PM, and one under a shared heading for the sub
        self.hidden = self.action(self.m, owner=self.pat, title='Chase the joinery final account', item=self.internal)
        self.open = self.action(self.m, owner=self.sid, title='Send the revised RAMS', item=self.shared,
                                due=self.today + timedelta(days=3))

    def test_template_carries_the_flag(self):
        self.assertEqual((self.shared.internal_only, self.internal.internal_only), (False, True))
        self.assertEqual(self.internal.heading, 'Commercial and costs')
        for line, out in (('Internal: Commercial', ('Commercial', True)), ('internal:Costs', ('Costs', True)),
                          ('Programme', ('Programme', False)), ('  Internal :  x', ('Internal :  x', False))):
            self.assertEqual(parse_agenda_line(line), out, line)

    def test_externals_never_see_internal_headings_minutes_or_actions(self):
        self.issue(self.m)
        for user in (self.carla, self.sid):
            self.login(user)
            r = self.client.get(self.m.get_absolute_url())
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, 'Hoarding to be moved Friday')
            self.assertNotContains(r, 'Commercial and costs')
            self.assertNotContains(r, SECRET)
            self.assertNotContains(r, 'Chase the joinery final account')
            r = self.client.get(self.url('meeting_list', self.pa.pk))
            self.assertNotContains(r, 'Chase the joinery final account')
        # Staff see it all, marked
        self.login(self.pat)
        r = self.client.get(self.m.get_absolute_url())
        self.assertContains(r, 'Commercial and costs')
        self.assertContains(r, SECRET)
        self.assertContains(r, 'Internal only')

    def test_the_email_leaves_out_internal_actions(self):
        mail.outbox = []
        self.issue(self.m)
        bodies = {m.to[0]: m.body for m in mail.outbox}
        self.assertIn('Send the revised RAMS', bodies['sid@s.test'])
        for body in bodies.values():
            self.assertNotIn(SECRET, body)
        self.assertNotIn('Chase the joinery final account', bodies['carla@c.test'])
        self.assertNotIn('Chase the joinery final account', bodies['sid@s.test'])

    def test_an_external_owner_under_an_internal_heading_never_sees_it(self):
        # Given to the sub before the heading was marked internal: it stays off their Needs me and email
        a = self.action(self.m, owner=self.sid, title='Price the extra bulkhead', item=self.shared)
        with org_context(self.org_a):
            AgendaItem.objects.filter(pk=self.shared.pk).update(internal_only=True)
        mail.outbox = []
        self.issue(self.m)
        self.assertFalse(any('Price the extra bulkhead' in m.body for m in mail.outbox))
        with org_context(self.org_a):
            refs = [i.ref for i in items(self.request_for(self.sid))]
        self.assertNotIn(a.reference, refs)
        self.assertNotIn(self.open.reference, refs)
        self.login(self.sid)
        self.assertEqual(self.client.post(self.url('meeting_action_update', self.pa.pk, a.pk),
                                          {'status': 'DONE'}).status_code, 404)

    def test_needs_me_for_externals_skips_internal_actions(self):
        hidden_sub = self.action(self.m, owner=self.sid, title='Internal one', item=self.internal)
        self.issue(self.m)
        with org_context(self.org_a):
            refs = [i.ref for i in items(self.request_for(self.sid))]
        self.assertIn(self.open.reference, refs)
        self.assertNotIn(hidden_sub.reference, refs)

    def test_staff_mark_a_heading_internal_on_the_minutes(self):
        self.login(self.pat)
        r = self.client.post(self.m.get_absolute_url(), {
            'action': 'minutes', 'version': self.fresh_meeting().version, 'internal_listed': '1',
            'internal': [str(self.shared.pk)], f'item-{self.shared.pk}': 'x'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            flags = dict(self.m.items.values_list('pk', 'internal_only'))
        self.assertEqual((flags[self.shared.pk], flags[self.internal.pk]), (True, False))

    def test_an_action_under_an_internal_heading_stays_with_our_team(self):
        self.login(self.pat)
        r = self.client.post(self.m.get_absolute_url(), {
            'action': 'add_action', 'act-title': 'Agree the margin', 'act-owner': self.sid.pk,
            'act-item': self.internal.pk}, follow=True)
        self.assertContains(r, 'its actions stay with our team')
        r = self.client.post(self.m.get_absolute_url(), {
            'action': 'add_action', 'act-title': 'Agree the margin', 'act-owner': self.alice.pk,
            'act-item': self.internal.pk})
        self.assertEqual(r.status_code, 302)

    def test_the_agenda_settings_take_the_prefix(self):
        self.login(self.alice)
        r = self.client.post(self.url('meeting_agendas'), {
            'kind': 'CLIENT', 'client-name': 'Client meeting', 'client-items': 'Progress\ninternal:  Costs\n'})
        self.assertEqual(r.status_code, 302)
        with org_context(self.org_a):
            t = AgendaTemplate.objects.get(kind='CLIENT')
        self.assertEqual(t.item_rows, [('Progress', False), ('Costs', True)])
        r = self.client.post(self.url('meeting_agendas'), {
            'kind': 'CLIENT', 'client-name': 'Client meeting', 'client-items': 'Internal:'})
        self.assertContains(r, 'Put the heading after')

    def fresh_meeting(self):
        with org_context(self.org_a):
            return Meeting.objects.get(pk=self.m.pk)


class MinutesDueTests(MeetingTestBase):
    def test_default_two_days_and_the_company_can_change_it(self):
        m = self.meeting()
        self.assertEqual(m.due_date, self.today + timedelta(days=2))
        self.login(self.alice)
        r = self.client.post(self.url('meeting_agendas'), {'kind': 'settings', 'settings-minutes_due_days': '5'})
        self.assertEqual(r.status_code, 302)
        m2 = self.meeting()
        self.assertEqual(m2.due_date, self.today + timedelta(days=5))
        with org_context(self.org_b):
            other = self.meeting(project=self.pb, org=self.org_b, actor=self.bob, attendees=[])
        self.assertEqual(other.due_date, self.today + timedelta(days=2))  # each company its own

    def test_settings_refuse_silly_numbers_and_are_for_admins(self):
        self.login(self.alice)
        r = self.client.post(self.url('meeting_agendas'), {'kind': 'settings', 'settings-minutes_due_days': '15'})
        self.assertContains(r, 'Enter 14 days or fewer.')
        for user, code in ((self.pat, 403), (self.carla, 403), (self.bob, 404)):
            self.assertEqual(self.status_for(user, self.url('meeting_agendas')), code, user)

    def test_no_organisation_fails_closed(self):
        with self.assertRaises(TenantContextMissing):
            list(MeetingSettings.objects.all())
