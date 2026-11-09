"""
Email is only a notification: no Reply-To on anything, a "<Company> via <product>" sender built
safely from the company's name, and a "replies aren't read" line with the item's link at the top.
"""
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr

from django.conf import settings
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from tenancy import notify
from tenancy.tests import Base

LINE = "Reply in Siteline, not to this email. Replies to this address aren't read."


@override_settings(PRODUCT_NAME='Siteline', DEFAULT_FROM_EMAIL='Siteline <no-reply@portal.test>',
                   SITE_URL='https://portal.test')
class NoReplyEmailTests(Base):
    def setUp(self):
        super().setUp()
        mail.outbox.clear()

    def only_email(self):
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0]

    def test_notification_starts_with_the_no_reply_line_and_the_link(self):
        notify.alert([self.bob], 'RFI-001 needs an answer', '/alpha/rfis/1/', org=self.org_a, actor=self.alice)
        m = self.only_email()
        self.assertEqual(m.body.splitlines()[:2], [LINE, 'https://portal.test/alpha/rfis/1/'])
        self.assertEqual(m.reply_to, [])
        self.assertEqual(m.from_email, 'Alpha Fitout via Siteline <no-reply@portal.test>')
        self.assertNotIn('Reply-To', m.message())

    def test_a_template_without_a_link_still_gets_one(self):
        notify.send('x@a.test', 'Your daily round-up', 'digest', {'user': self.alice, 'waiting': []}, org=self.org_a)
        self.assertEqual(self.only_email().body.splitlines()[:2], [LINE, f'https://portal.test/{self.org_a.slug}/'])

    def test_email_with_no_company_keeps_the_default_sender(self):
        notify.send('x@a.test', 'Hello', 'notification', {'title': 'Hello', 'link': 'https://portal.test/x/'})
        m = self.only_email()
        self.assertEqual(m.from_email, 'Siteline <no-reply@portal.test>')
        self.assertTrue(m.subject.startswith('Hello'))
        self.assertEqual(notify.sender_for(None), 'Siteline <no-reply@portal.test>')

    def test_hostile_company_name_cannot_inject_headers(self):
        self.org_a.name = 'Evil "Co" <boss@evil.test>\r\nBcc: victim@evil.test\nReply-To: me@evil.test\\' + 'x' * 200
        self.org_a.save()
        notify.alert([self.bob], 'Something', '/alpha/', org=self.org_a, actor=self.alice)
        m = self.only_email()
        name, address = parseaddr(m.from_email)
        self.assertEqual(address, 'no-reply@portal.test')
        for bad in '\r\n"<>\\':
            self.assertNotIn(bad, name)
        self.assertTrue(name.endswith(' via Siteline'))
        self.assertLessEqual(len(name), notify.SENDER_NAME_MAX + len(' via Siteline'))
        for bad in '\r\n<>':
            self.assertNotIn(bad, m.subject)
        # The message really goes, and the headers are only the ones we set
        raw = message_from_bytes(m.message().as_bytes())
        self.assertIsNone(raw['Bcc'])
        self.assertIsNone(raw['Reply-To'])
        self.assertEqual(m.recipients(), ['bob@b.test'])
        self.assertEqual(parseaddr(raw['From'])[1], 'no-reply@portal.test')

    def test_non_ascii_company_name_is_encoded(self):
        self.org_a.name = 'Café Interiors'
        self.org_a.save()
        notify.alert([self.bob], 'Something', '/alpha/', org=self.org_a, actor=self.alice)
        raw = message_from_bytes(self.only_email().message().as_bytes())
        self.assertEqual(str(make_header(decode_header(raw['From']))), 'Café Interiors via Siteline <no-reply@portal.test>')

    def test_a_name_with_nothing_left_falls_back_to_the_default(self):
        self.org_a.name = '"<>"'
        self.assertEqual(notify.sender_for(self.org_a), 'Siteline <no-reply@portal.test>')

    def test_team_invite_keeps_its_wording_and_points_to_support(self):
        self.login(self.alice)
        self.client.post(self.url('team', self.org_a), {'email': 'new@a.test', 'role': 'MEMBER'})
        m = self.only_email()
        self.assertTrue(m.body.startswith('Hi,'))
        self.assertIn("Replies to this address aren't read.", m.body)
        self.assertIn(settings.SUPPORT_EMAIL, m.body)
        self.assertEqual(m.reply_to, [])
        self.assertEqual(m.from_email, 'Alpha Fitout via Siteline <no-reply@portal.test>')

    def test_project_invite_points_to_support(self):
        self.login(self.alice)
        self.client.post(self.url('project_team', self.org_a, self.pa.pk), {
            'action': 'invite', 'email': 'client@x.test', 'role': 'CLIENT', 'company_name': 'X Ltd'})
        m = self.only_email()
        self.assertIn("Replies to this address aren't read.", m.body)
        self.assertIn(settings.SUPPORT_EMAIL, m.body)
        self.assertNotIn(LINE, m.body)
        self.assertEqual(m.reply_to, [])

    def test_password_reset_has_no_reply_to_and_points_to_support(self):
        self.client.post(reverse('password_reset'), {'email': 'alice@a.test'})
        m = self.only_email()
        self.assertEqual(m.reply_to, [])
        self.assertIn("Replies to this address aren't read.", m.body)
        self.assertIn(settings.SUPPORT_EMAIL, m.body)
        self.assertEqual(m.from_email, 'Siteline <no-reply@portal.test>')
