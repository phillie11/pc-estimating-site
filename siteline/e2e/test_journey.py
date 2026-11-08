"""
One job, lead to final account, through every module, by HTTP only.

Set-up uses the ORM (a company, its staff, two subcontractors in the directory, a client's
email address). After that every step is a GET or POST through the Django test client as
the person who would do it: estimator, project manager, owner, subcontractor, client. The
assertions read what each of them sees on the page (and the Needs me list behind the
badge), not just status codes, so a change in one module that breaks the next one's view
of the job fails here.

The steps follow the brief:
  1  lead -> bid/no-bid gate -> tendering, package invited
  2  subcontractors quote through the portal
  3  quote levelling: scope from an .xlsx, carry with a reason, submit
  4  won -> handover -> appoint
  5  contract set-up (JCT IC) -> budget from the tender -> order committed from the package
  6  procurement schedule line -> submittal C -> resubmission -> A -> ordered
  7  site diary day with a delay and a photo
  8  drawing uploaded and shared -> snag pinned in the viewer -> the sub sees it
  9  RFI -> change event -> client variation -> client approves
  10 AP-001 -> client payment notice -> payment recorded
  11 sub applies -> our payment notice -> paid with CIS -> cost report reconciles
  Release 3:
  12 inspection with a failed check -> snag for the sub -> fixed and closed
  13 hot works permit asked for by the PM -> approved by the owner (never by whoever asked) -> closed
  14 fire-stopping recorded with photos -> signed off by the PM
  15 near miss reported -> investigated with an action -> closed; a visitor signs in by the QR poster
  16 MS Project XML import -> re-import updates by task ID -> the 2/3/4 week look-ahead for each role
  17 meeting with minutes and actions -> issued by email link -> actions done
  18 practical completion -> client defect fixed -> O&M from the sub -> handover pack with the fire-stopping register
  19 daywork sheet priced and signed on a phone -> change register -> client variation
  20 wave 2: the sub records fire-stopping in the portal (failed, resealed, signed off by the PM) and reports a
     near miss; the client signs a daywork sheet in their portal; a private then shared drawing mark-up; an
     API token reads this company's job and not another's; the portfolio margin report agrees with the CVR
  21 the month ends -> CVR signed off -> the cashflow adds up to the final account and final cost
  Needs me for each role is checked after every step (checkpoint), and inside the steps as things move.
"""
import csv
import hashlib
import io
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.views import create_organization
from directory.models import Subcontractor, Trade
from tenancy.context import org_context
from tenancy.models import Membership

PW = 'Journey-Pass-2026'
TODAY = timezone.localdate


def png_bytes():
    """A real 8x8 PNG, so Pillow can make thumbnails."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), (200, 30, 30)).save(buf, 'PNG')
    return buf.getvalue()


def signature_png():
    """A signature as the phone's signature pad sends it: a PNG with a line drawn on it."""
    from PIL import Image, ImageDraw
    img = Image.new('RGBA', (300, 120), (255, 255, 255, 255))
    ImageDraw.Draw(img).line((20, 80, 120, 30, 200, 90), fill=(21, 25, 31, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def pdf_bytes(pages=1):
    """A small valid PDF (ReportLab is already a dependency)."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, f'GA plan sheet {i + 1}')
        c.showPage()
    c.save()
    return buf.getvalue()


def scope_xlsx():
    """Our scope lines for the joinery trade, as an estimator would have them in Excel."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(['Ref', 'Description', 'Qty', 'Unit'])
    ws.append(['J1', 'Bar front in oak veneer', 1, 'item'])
    ws.append(['J2', 'Back bar shelving', 12, 'm'])
    ws.append(['J3', 'Window boards', 8, 'nr'])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def text(response):
    """The page as plain text (tags out, whitespace collapsed), for 'what the person reads' checks."""
    html = response.content.decode()
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.S)
    return ' '.join(re.sub(r'<[^>]+>', ' ', html).replace('&nbsp;', ' ').replace('&#x27;', "'")
                    .replace('&amp;', '&').replace('&quot;', '"').split())


class _Forms(HTMLParser):
    """Every <form> on a page with the values a browser would submit untouched."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.cur, self.select, self.textarea, self.opt_first = [], None, None, None, {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self.cur = {'action': a.get('action', ''), 'fields': [], 'names': set()}
            self.forms.append(self.cur)
        if self.cur is None:
            return
        name = a.get('name')
        if tag == 'input' and name:
            self.cur['names'].add(name)
            kind = a.get('type', 'text')
            if kind in ('checkbox', 'radio'):
                if 'checked' in a:
                    self.cur['fields'].append((name, a.get('value', 'on')))
            elif kind not in ('submit', 'button', 'file', 'image', 'reset'):
                self.cur['fields'].append((name, a.get('value', '')))
            elif kind == 'submit' and name == 'action':
                self.cur['names'].add('action=' + a.get('value', ''))
        elif tag == 'button' and name == 'action':
            self.cur['names'].add('action=' + a.get('value', ''))
        elif tag == 'select' and name:
            self.cur['names'].add(name)
            self.select, self.opt_first[name] = name, None
            self.selected = False
        elif tag == 'option' and self.select:
            val = a.get('value', '')
            if self.opt_first[self.select] is None:
                self.opt_first[self.select] = val
            if 'selected' in a:
                self.cur['fields'].append((self.select, val))
                self.selected = True
        elif tag == 'textarea' and name:
            self.cur['names'].add(name)
            self.textarea = [name, '']

    def handle_data(self, data):
        if self.textarea:
            self.textarea[1] += data

    def handle_endtag(self, tag):
        if tag == 'select' and self.select:
            if not self.selected and self.opt_first.get(self.select) is not None:
                self.cur['fields'].append((self.select, self.opt_first[self.select]))
            self.select = None
        elif tag == 'textarea' and self.textarea:
            self.cur['fields'].append((self.textarea[0], self.textarea[1].lstrip('\n')))
            self.textarea = None
        elif tag == 'form':
            self.cur = None


def form_data(response, having):
    """The values of the first form on the page with a field (or action button value) `having`."""
    p = _Forms()
    p.feed(response.content.decode())
    for f in p.forms:
        if having in f['names']:
            data = {}
            for k, v in f['fields']:
                if k in data:
                    data[k] = (data[k] if isinstance(data[k], list) else [data[k]]) + [v]
                else:
                    data[k] = v
            data.pop('csrfmiddlewaretoken', None)
            if having.startswith('action='):
                data['action'] = having.split('=', 1)[1]
            return data
    raise AssertionError(f'No form with {having!r} on the page')


# The test client sends every POST as multipart, so the upload rate limit (60 in ten minutes per person) would
# count each form the PM submits; nobody posts that many in ten minutes. security/tests cover the limits.
@override_settings(EMAIL_SYNC=True, RATELIMIT_ENABLED=False)
class JourneyTest(TestCase):
    """Phases share state, so they run in order inside one test."""

    # ------------------------------------------------------------------ set-up (ORM)

    def setUp(self):
        self.owner = User.objects.create_user(email='owner@journey.test', password=PW, full_name='Olive Owner')
        self.est = User.objects.create_user(email='est@journey.test', password=PW, full_name='Ezra Estimator')
        self.pm = User.objects.create_user(email='pm@journey.test', password=PW, full_name='Priya Manager')
        self.org = create_organization('Journey Fitout', self.owner)
        # A paying company: the journey runs past the month end (phase 21), beyond the free trial
        self.org.subscription_status = self.org.STATUS_ACTIVE
        self.org.save(update_fields=['subscription_status'])
        Membership.objects.create(organization=self.org, user=self.est, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_ESTIMATING)
        Membership.objects.create(organization=self.org, user=self.pm, role=Membership.ROLE_MEMBER,
                                  area=Membership.AREA_DELIVERY)
        # A second company, to show nothing of this job leaks across
        self.rival = User.objects.create_user(email='rival@other.test', password=PW, full_name='Rhys Rival')
        self.other_org = create_organization('Other Build', self.rival)
        with org_context(self.org):
            self.joinery = Trade.objects.get(name='Joinery')
            self.oak = Subcontractor.objects.create(company_name='Oak & Ash Joinery', email='sid@oakash.test',
                                                    contact_name='Sid Oak', status=Subcontractor.STATUS_APPROVED)
            self.oak.trades.add(self.joinery)
            self.cheap = Subcontractor.objects.create(company_name='Cheapside Joinery', email='cal@cheapside.test',
                                                      contact_name='Cal Cheap', status=Subcontractor.STATUS_APPROVED)
            self.cheap.trades.add(self.joinery)
        self.c = {}
        for name, user in (('owner', self.owner), ('est', self.est), ('pm', self.pm), ('rival', self.rival)):
            self.c[name] = self._client_for(user)

    def travel_to(self, day):
        """
        Move the clock to 9am on `day` for the rest of the journey (the month end, for the CVR). Everything reads
        the time through django.utils.timezone, so dates on pages, Needs me and the rules all move together.
        Sessions older than two weeks would have expired by then, so everyone signs in again.
        """
        real_now = timezone.now
        target = timezone.make_aware(datetime.combine(day, time(9)))
        offset = target - real_now()
        patcher = mock.patch('django.utils.timezone.now', side_effect=lambda: real_now() + offset)
        patcher.start()
        self.addCleanup(patcher.stop)
        emails = {'owner': 'owner@journey.test', 'est': 'est@journey.test', 'pm': 'pm@journey.test',
                  'rival': 'rival@other.test', 'sub': 'sid@oakash.test', 'sub2': 'cal@cheapside.test',
                  'client': 'cara@northerntaverns.test'}
        for name in list(self.c):
            self.c[name] = self._client_for(User.objects.get(email=emails[name]))
        self.assertEqual(TODAY(), day)

    def _client_for(self, user):
        c = Client()
        c.force_login(user, backend='django.contrib.auth.backends.ModelBackend')
        return c

    # ------------------------------------------------------------------ helpers

    def u(self, name, *args):
        return reverse(name, args=[self.org.slug, *args])

    def pu(self, name, *args):
        return reverse(name, args=[self.org.slug, self.pk, *args])

    def get(self, who, url, status=200):
        r = self.c[who].get(url)
        self.assertEqual(r.status_code, status, f'{who} GET {url} gave {r.status_code}')
        return r

    def post(self, who, url, data=None, status=302, follow=False):
        r = self.c[who].post(url, data or {}, follow=follow)
        if r.status_code != status:
            self.fail(f'{who} POST {url} gave {r.status_code}, expected {status}.\n{self._why(r)}')
        return r

    def _why(self, r):
        """Form errors and messages from a response that didn't go where expected."""
        out = []
        for ctx in (r.context or []):
            for key, val in ctx.flatten().items() if hasattr(ctx, 'flatten') else []:
                errs = getattr(val, 'errors', None)
                if errs and hasattr(val, 'is_bound'):
                    out.append(f'{key}: {errs.as_data() if hasattr(errs, "as_data") else errs}')
            break
        msgs = [str(m) for m in r.context['messages']] if r.context and 'messages' in r.context else []
        return '\n'.join(out + msgs) or text(r)[:1500]

    def messages_after(self, response):
        return [str(m) for m in response.context['messages']] if response.context else []

    def needs(self, who):
        """(badge count, {(kind, ref or title)}) as the person sees them on Needs me."""
        r = self.get(who, self.u('needs_me'))
        m = re.search(r'aria-label="Needs me: (\d+) waiting"', r.content.decode())
        badge = int(m.group(1)) if m else 0
        items = r.context['items']
        self.assertEqual(badge, r.context['total_count'], f'{who}: badge and list disagree')
        return badge, {(i.kind, i.title if i.kind == 'diary_missing' else (i.ref or i.title)) for i in items}

    def assertNeeds(self, who, expected, ignore_kinds=('diary_missing',)):
        """Exactly these Needs me items (kind, ref-or-title) for this person, plus any of the ignored kinds."""
        badge, items = self.needs(who)
        got = {i for i in items if i[0] not in ignore_kinds}
        # After the month end (phase 21), next month's applications wait too, if they fall due this week
        expected = set(expected) | getattr(self, 'month_end_needs', {}).get(who, set())
        self.assertEqual(got, expected, f'Needs me for {who}')
        return badge

    # ------------------------------------------------------------------ the journey

    def test_one_job_through_every_module(self):
        phases = [self.phase1_lead_to_tender, self.phase2_subs_quote, self.phase3_levelling_and_submit,
                  self.phase4_won_handover_appoint, self.phase5_contract_budget_order, self.phase6_procurement,
                  self.phase7_site_diary, self.phase8_drawing_snag_pin, self.phase9_rfi_change_variation,
                  self.phase10_application_for_payment, self.phase11_sub_payment_and_cost_report,
                  self.phase12_inspection_fails_and_raises_a_snag, self.phase13_hot_works_permit,
                  self.phase14_fire_stopping, self.phase15_incident_and_visitor_sign_in,
                  self.phase16_programme_import_and_look_ahead, self.phase17_meeting_issued_and_actions_done,
                  self.phase18_practical_completion_defects_om_and_handover,
                  self.phase19_daywork_to_the_change_register, self.phase20_portal_markup_api_and_reports,
                  self.phase21_month_end_cvr_signed_off_and_cashflow]
        for n, phase in enumerate(phases, 1):
            phase()
            self.checkpoint(n)

    def checkpoint(self, n):
        out = {}
        for who in ('owner', 'est', 'pm', 'sub', 'sub2', 'client', 'rival'):
            if who not in self.c:
                continue
            if who == 'rival':
                continue
            badge, items = self.needs(who)
            out[who] = {i for i in items if i[0] != 'diary_missing'}
            extra = len(self._diary_missing()) if who == 'pm' and n >= 4 else 0  # the PM's live job from phase 4
            self.assertEqual(badge, len(out[who]) + extra, f'badge for {who} after phase {n}')
        expected = {who: set() for who in out}
        expected.update(self.EXPECTED_NEEDS.get(n, {}))
        if 5 <= n <= 9:  # from the contract's valuation day until AP-001 goes in
            expected['pm'] = expected.get('pm', set()) | {self.ap_due()}
        if n == 21:  # next month's applications, ours and the sub's, if their valuation dates are this week
            for who, due in self.month_end_needs.items():
                expected[who] = expected.get(who, set()) | due
        self.assertEqual(out, expected, f'Needs me after phase {n}')

    # Needs me for every role once each phase has finished (diary days aside, checked by count above).
    # Everything else is cleared by the phase that raised it, and is asserted there when it was waiting.
    # The client doesn't acknowledge the drawing issue until they take the handover (phase 18).
    EXPECTED_NEEDS = {n: {'client': {('transmittal', 'TR-001')}} for n in range(8, 18)}

    def ap_due(self):
        """The PM's prompt to submit this month's application: the valuation day is today's day of the month."""
        return ('payment', f'Application for payment for {TODAY().day} {TODAY():%b %Y}')

    # 1 -----------------------------------------------------------------

    def phase1_lead_to_tender(self):
        due = TODAY() + timedelta(days=21)
        r = self.post('est', self.u('project_create'), {
            'name': 'Leeds Bar Fit-out', 'client_name': 'Northern Taverns Ltd', 'site_address': '1 Call Lane, Leeds LS1',
            'sector': '', 'stage': 'LEAD', 'owner': self.est.pk, 'value': '250000', 'probability': '50',
            'tender_received': TODAY().isoformat(), 'tender_due': (due + timedelta(days=7)).isoformat(),
            'quotes_due': due.isoformat(), 'lead_source': '', 'description': 'Strip out and fit out a city bar.',
        })
        from projects.models import Project
        with org_context(self.org):
            self.project = Project.objects.get(name='Leeds Bar Fit-out')
        self.pk = self.project.pk
        self.assertEqual(r.url, self.project.get_absolute_url())
        self.assertIn('Leeds Bar Fit-out', text(self.get('est', self.u('pipeline'))))
        # The PM (projects only) can't open a bid
        self.get('pm', self.pu('project_detail'), status=404)

        # Lead -> Tendering goes through the bid/no-bid gate
        r = self.post('est', self.pu('project_stage'), {'stage': 'TENDERING'})
        self.assertIn(reverse('bid_gate', args=[self.org.slug, self.pk]), r.url)
        page = text(self.get('est', r.url))
        self.assertIn('Answer the six questions before it moves to Tendering', page)
        answers = {f'q_{k}': '4' for k in ('client', 'sector', 'value', 'programme', 'workload', 'terms')}
        r = self.post('est', self.pu('bid_gate'), {
            'action': 'go', 'to': 'TENDERING', 'from': 'LEAD', 'contract_form': 'JCT_IC', 'bidders': '4',
            'client_budget': '260000', 'amendments': '', 'decision_note': '', **answers})
        self.assertEqual(r.url, self.pu('tender'))
        page = text(self.get('est', r.url))
        self.assertIn('Tendering', page)

        # Invite both joiners to price the package
        r = self.post('est', self.pu('package_list'), {
            'action': 'invite', 'trade': self.joinery.pk, 'subcontractors': [self.oak.pk, self.cheap.pk],
            'scope': 'Bar front, back bar and window boards per drawings.', 'quote_due': due.isoformat()})
        page = text(self.get('est', self.pu('package_list')))
        self.assertIn('Oak & Ash Joinery', page)
        self.assertIn('Cheapside Joinery', page)
        # Both are new to the portal: each gets one email with a link, never attachments
        invites = [m for m in mail.outbox if 'Invitation to tender' in m.subject]
        self.assertEqual(sorted(m.to[0] for m in invites), ['cal@cheapside.test', 'sid@oakash.test'])
        self.assertFalse(any(m.attachments for m in invites))
        self.invite_links = {m.to[0]: re.search(r'https?://[^\s]+/invite/project/[^\s/]+/', m.body).group(0)
                             for m in invites}

    # 2 -----------------------------------------------------------------

    def _accept(self, email, name):
        link = self.invite_links[email]
        path = re.sub(r'^https?://[^/]+', '', link)
        c = Client()
        r = c.post(path, {'full_name': name, 'password': PW})
        self.assertEqual(r.status_code, 302, text(r)[:800])
        return c

    def phase2_subs_quote(self):
        self.c['sub'] = self._accept('sid@oakash.test', 'Sid Oak')
        self.c['sub2'] = self._accept('cal@cheapside.test', 'Cal Cheap')
        self.sub_user = User.objects.get(email='sid@oakash.test')
        # A tendering sub sees only their tender page, and it waits on them
        badge, items = self.needs('sub')
        self.assertEqual({k for k, _ in items}, {'tender'})
        self.assertEqual(badge, 1)
        page = text(self.get('sub', self.pu('tender')))
        self.assertIn('Bar front, back bar and window boards', page)
        self.assertNotIn('Cheapside', page)  # never other bidders
        self.get('sub', self.pu('rfi_list'), status=403)
        from directory.models import Package
        with org_context(self.org):
            self.pkg_oak = Package.objects.get(project=self.project, subcontractor=self.oak)
            self.pkg_cheap = Package.objects.get(project=self.project, subcontractor=self.cheap)
        quote_pdf = SimpleUploadedFile('oak-quote.pdf', pdf_bytes(), content_type='application/pdf')
        self.post('sub', self.pu('tender'), {
            'action': 'quote', 'package': self.pkg_oak.pk, 'quote_value': '48500.00', 'quote_file': quote_pdf,
            'exclusions': 'Ironmongery', 'valid_until': (TODAY() + timedelta(days=90)).isoformat(),
            'programme_weeks': '6', 'quote_notes': ''})
        self.post('sub2', self.pu('tender'), {
            'action': 'quote', 'package': self.pkg_cheap.pk, 'quote_value': '44000.00',
            'exclusions': 'Window boards', 'valid_until': (TODAY() + timedelta(days=60)).isoformat(),
            'programme_weeks': '8', 'quote_notes': ''})
        # Quoted: nothing waits on the sub any more
        self.assertEqual(self.needs('sub')[0], 0)
        page = text(self.get('est', self.pu('package_list')))
        self.assertIn('48,500', page)
        self.assertIn('44,000', page)

    # 3 -----------------------------------------------------------------

    def phase3_levelling_and_submit(self):
        sheet = reverse('levelling_sheet', args=[self.org.slug, self.pk, self.joinery.pk])
        self.get('pm', sheet, status=404)  # estimating only while it's a bid
        imp = reverse('levelling_import', args=[self.org.slug, self.pk, self.joinery.pk])
        self.post('est', imp, {'imp-kind': 'scope',
                               'imp-file': SimpleUploadedFile('joinery-scope.xlsx', scope_xlsx())})
        page = text(self.get('est', sheet))
        for d in ('Bar front in oak veneer', 'Back bar shelving', 'Window boards'):
            self.assertIn(d, page)
        from levelling.models import ScopeLine
        with org_context(self.org):
            lines = {l.ref: l.pk for l in ScopeLine.objects.filter(sheet__project=self.project)}
        rates = lambda pkg, data: self.post(  # noqa: E731
            'est', reverse('levelling_rates', args=[self.org.slug, self.pk, self.joinery.pk, pkg.pk]), data)
        # Oak: priced everything; Cheapside left window boards out
        rates(self.pkg_oak, {f'sum_{lines["J1"]}': '30000', f'rate_{lines["J2"]}': '1000', f'rate_{lines["J3"]}': '812.50'})
        rates(self.pkg_cheap, {f'sum_{lines["J1"]}': '29000', f'rate_{lines["J2"]}': '1250'})
        page = text(self.get('est', sheet))
        self.assertIn('48,500', page)
        # Oak's quote excludes ironmongery: the estimator prices it in, so Oak levels at 48,500 + 1,250 = 49,750
        self.post('est', sheet, {'action': 'exclusion', 'exc-package': self.pkg_oak.pk, 'exc-text': 'Ironmongery',
                                 'exc-status': 'PRICED', 'exc-amount': '1250.00'})
        page = text(self.get('est', sheet))
        self.assertIn('49,750', page)
        # Carrying Oak, not the lowest levelled, needs a reason
        r = self.post('est', sheet, {'action': 'carry', 'carry-package': self.pkg_oak.pk, 'carry-reason': ''}, status=200)
        self.assertIn('Say why', text(r))
        self.post('est', sheet, {'action': 'carry', 'carry-package': self.pkg_oak.pk,
                                 'carry-reason': 'Cheapside exclude window boards; Oak proven on similar bars'})
        self.assertIn('Carried: Oak & Ash Joinery',
                      text(self.get('est', reverse('levelling_index', args=[self.org.slug, self.pk]))))
        # Tendering -> Submitted (the gate was answered, so it goes straight through)
        r = self.post('est', self.pu('project_stage'), {'stage': 'SUBMITTED'})
        self.assertEqual(r.url, self.project.get_absolute_url())
        self.assertIn('Submitted', text(self.get('est', self.pu('project_detail'))))

    # 4 -----------------------------------------------------------------

    def phase4_won_handover_appoint(self):
        start = self.job_start = TODAY() - timedelta(days=30)
        finish = TODAY() + timedelta(days=120)
        # Moving to Won from the stage select goes through the handover
        r = self.post('est', self.pu('project_stage'), {'stage': 'WON'})
        self.assertEqual(r.url, self.pu('project_handover'))
        page = text(self.get('est', r.url))
        self.assertIn('Oak & Ash Joinery', page)  # quotes carried across, with who was carried
        self.post('est', self.pu('project_handover'), {
            'manager': self.pm.pk, 'value': '250000.00', 'start_date': start.isoformat(),
            'completion_date': finish.isoformat(), 'handover_notes': 'Client wants the bar open for Christmas.'})
        # The PM is told, and the job is now theirs and not the estimator's
        self.assertIn('Handed over to you: Leeds Bar Fit-out', text(self.get('pm', self.u('notifications'))))
        self.get('est', self.pu('project_detail'), status=404)
        page = text(self.get('pm', self.pu('project_detail')))
        self.assertIn('Client wants the bar open for Christmas', page)
        # The PM appoints the carried subcontractor
        page = text(self.get('pm', self.pu('package_list')))
        self.assertIn('Carried in tender', page)
        self.post('pm', self.pu('package_list'), {'action': 'appoint', 'package': self.pkg_oak.pk})
        self.assertIn('You have been appointed: Joinery on Leeds Bar Fit-out',
                      text(self.get('sub', self.u('notifications'))))
        # The unsuccessful bidder is told and loses the project
        self.assertIn('your quote was not successful', text(self.get('sub2', self.u('notifications'))))
        self.assertEqual(self.needs('sub2'), (0, set()))
        self.get('sub2', self.pu('project_detail'), status=404)
        # Into Live, and invite the client
        self.post('pm', self.pu('project_stage'), {'stage': 'LIVE'})
        mail.outbox.clear()
        self.post('pm', self.pu('project_team'), {'action': 'invite', 'email': 'cara@northerntaverns.test',
                                                  'role': 'CLIENT', 'company_name': 'Northern Taverns Ltd',
                                                  'message': ''})
        link = re.search(r'https?://[^\s]+/invite/project/[^\s/]+/', mail.outbox[0].body).group(0)
        self.invite_links['cara@northerntaverns.test'] = link
        self.c['client'] = self._accept('cara@northerntaverns.test', 'Cara Client')
        page = text(self.get('client', self.pu('project_detail')))
        self.assertIn('Leeds Bar Fit-out', page)
        self.assertIn('Oak & Ash Joinery', page)  # who's who: the appointed sub
        # Commercial figures, handover notes and who else priced the work stay internal
        for secret in ('48,500', '49,750', '250,000', 'Christmas', 'Cal Cheap', 'Cheapside'):
            self.assertNotIn(secret, page)
        # The sub now has the live-job portal, and can't tell who they beat
        page = text(self.get('sub', self.pu('project_detail')))
        self.assertIn('Leeds Bar Fit-out', page)
        for secret in ('250,000', 'Christmas', 'Cal Cheap', 'Cheapside'):
            self.assertNotIn(secret, page)
        # Staff still see the bidder joining
        self.assertIn('Cal Cheap joined the project', text(self.get('pm', self.u('activity'))))
        # Safety: the job started without a construction phase plan, so it is on the PM's Needs me until uploaded
        _, items = self.needs('pm')
        self.assertIn(('safety', 'CDM'), items)
        self.post('pm', self.pu('phase_plan_upload'), {
            'file': SimpleUploadedFile('cpp.pdf', b'%PDF-1.4 plan', content_type='application/pdf')})
        _, items = self.needs('pm')
        self.assertNotIn(('safety', 'CDM'), items)
        # The appointed sub signs in on site from the QR poster with one tap, but can't open the Safety tab
        poster = text(self.get('pm', self.pu('safety_poster')))
        signin = re.search(r'/sign-in/[\w-]+/', poster).group(0)
        self.assertIn('Sign in as', text(self.get('sub', signin)))
        self.post('sub', signin, {'me': '1'})
        self.assertIn('Oak & Ash Joinery', text(self.get('pm', self.pu('safety_on_site'))))
        self.get('sub', self.pu('safety_home'), status=403)

    # 5 -----------------------------------------------------------------

    def phase5_contract_budget_order(self):
        self.get('client', self.pu('contract_detail'), status=403)
        r = self.get('pm', self.pu('contract_edit'))
        data = form_data(r, 'form')
        data.update({'form': 'JCT_IC', 'edition': '2016', 'contract_sum': '250000.00', 'retention_percent': '',
                     'valuation_day': '', 'due_date_offset_days': '', 'final_date_offset_days': '',
                     'pay_less_notice_days': '', 'payment_notice_days': '', 'defects_period_months': ''})
        # JCT IC pays from Interim Valuation Dates, so the day of the month is needed and never guessed
        r = self.post('pm', self.pu('contract_edit'), data, status=200)
        self.assertIn('Enter the valuation day', text(r))
        self.valuation_day = TODAY().day
        data['valuation_day'] = str(self.valuation_day)
        r = self.post('pm', self.pu('contract_edit'), data, follow=True, status=200)
        page = text(r)
        self.assertIn('Contract set up', page)
        self.assertIn('JCT Intermediate', page)
        self.assertIn('advisory', page.lower())
        self.assertIn(f'Day {TODAY().day} of each month', page)
        # With the day set, this month's application is on the PM's Needs me
        self.assertNeeds('pm', {self.ap_due()})
        # Budget from the tender: 250,000 less 10% = 225,000, spread over codes
        r = self.get('pm', self.pu('cost_budget_setup'))
        codes = r.context['form'].codes
        self.joinery_code = next(c for c in codes if c.name.lower() == 'joinery')
        self.prelims_code = codes[0]
        self.post('pm', self.pu('cost_budget_setup'), {
            'basis_value': '250000.00', 'margin_pct': '10', 'reason': 'From tender build-up',
            f'amt_{self.joinery_code.pk}': '50000.00', f'amt_{self.prelims_code.pk}': '175000.00'})
        page = text(self.get('pm', self.pu('cost_report')))
        self.assertIn('225,000', page)
        # The order, committed from the appointed package at the levelled total, with the quote shown alongside
        r = self.get('pm', self.pu('cost_commitments'))
        page = text(r)
        self.assertIn('Oak & Ash Joinery', page)
        self.assertIn('£49,750.00 Levelled total · quoted £48,500.00', page)
        data = form_data(r, 'cost_code')
        self.assertEqual(int(data['package']), self.pkg_oak.pk)
        self.assertEqual(int(data['cost_code']), self.joinery_code.pk)  # guessed from the trade
        r = self.post('pm', self.pu('cost_commit_package'), data, follow=True, status=200)
        page = text(r)
        self.assertIn('ORD-001 committed: Oak & Ash Joinery at £49,750.00 (the levelled total from quote levelling)',
                      page)
        self.assertIn('49,750.00', page)
        self.assertIn('Joinery subcontract', page)
        with org_context(self.org):
            order = self.pkg_oak.commitment
        page = text(self.get('pm', order.get_absolute_url()))
        self.assertIn('Placed at £49,750.00 Levelled total from quote levelling', page)
        self.assertIn('Original quote £48,500.00', page)

    # 6 -----------------------------------------------------------------

    def phase6_procurement(self):
        self.assertNeeds('pm', {self.ap_due()})
        self.assertNeeds('sub', set())
        # A schedule line from the package: required on site in 4 weeks + 3 days, 4 weeks' lead time
        r = self.get('pm', self.pu('procurement_line_create') + f'?package={self.pkg_oak.pk}')
        data = form_data(r, 'title')
        self.assertEqual(data['title'], 'Joinery')
        data.update({'required_on_site': (TODAY() + timedelta(days=31)).isoformat(), 'lead_time_weeks': '4',
                     'approval_weeks': '0', 'long_lead': 'on'})
        self.post('pm', self.pu('procurement_line_create'), data)
        page = text(self.get('pm', self.pu('procurement_schedule')))
        self.assertIn('Joinery', page)
        self.assertIn('Oak & Ash Joinery', page)
        # Order-by is within a week, so it is on the PM's Needs me
        self.assertNeeds('pm', {('order_by', 'Order Joinery'), self.ap_due()})
        self.get('client', self.pu('procurement_schedule'), status=403)  # clients see nothing of procurement

        # The sub sees the line coming up in their portal and sends shop drawings
        r = self.get('sub', self.pu('submittal_list'))
        self.assertIn('Joinery', text(r))
        line_id = int(form_data(r, 'line')['line'])
        r = self.post('sub', self.pu('submittal_list'), {
            'action': 'start', 'line': line_id, 'kind': 'SHOP_DRAWING', f'l{line_id}-note': 'Rev 01 for review',
            f'l{line_id}-files': SimpleUploadedFile('bar-front-shop-dwg.pdf', pdf_bytes())})
        sub_url = r.url
        page = text(self.get('pm', sub_url))
        self.assertIn('SUB-001', page)
        self.assertIn('bar-front-shop-dwg.pdf', page)
        self.assertNeeds('pm', {('order_by', 'Order Joinery'), ('submittal', 'SUB-001'), self.ap_due()})
        self.assertNeeds('sub', set())

        # C: revise and resubmit, which puts the ball back with the sub
        r = self.post('pm', sub_url, {'action': 'review', 'code': 'C', 'comments': ''}, status=200)
        self.assertIn('Say what needs to change', text(r))
        self.post('pm', sub_url, {'action': 'review', 'code': 'C', 'comments': 'Show the LED channel detail.'})
        page = text(self.get('sub', sub_url))
        self.assertIn('Show the LED channel detail', page)
        self.assertNeeds('sub', {('submittal', 'SUB-001')})
        self.assertNeeds('pm', {('order_by', 'Order Joinery'), self.ap_due()})
        # Can't order while a submittal waits for an A or B
        r = self.post('pm', reverse('procurement_line', args=[self.org.slug, self.pk, line_id]),
                      {'action': 'ordered'}, follow=True, status=200)
        self.assertIn('SUB-001 needs an A or B before this can be ordered', text(r))

        # Rev 02, then A
        self.post('sub', sub_url, {'action': 'submit', 'note': 'LED channel added',
                                   'files': SimpleUploadedFile('bar-front-shop-dwg-02.pdf', pdf_bytes())})
        self.assertNeeds('sub', set())
        self.post('pm', sub_url, {'action': 'review', 'code': 'A', 'comments': ''})
        page = text(self.get('sub', sub_url))
        self.assertIn('A: Approved', page)
        self.assertIn('Approved', text(self.get('pm', self.pu('procurement_schedule'))))
        self.post('pm', reverse('procurement_line', args=[self.org.slug, self.pk, line_id]), {'action': 'ordered'})
        self.assertIn('Ordered', text(self.get('pm', self.pu('procurement_schedule'))))
        self.assertNeeds('pm', {self.ap_due()})

    # 7 -----------------------------------------------------------------

    def _diary_missing(self):
        """The diary days the rules say are due and unwritten (diary.rules, a pure function), as Needs me titles."""
        from diary import rules
        from diary.models import DiaryEntry
        now = timezone.localtime().replace(tzinfo=None)
        with org_context(self.org):
            written = set(DiaryEntry.objects.filter(project_id=self.pk).values_list('entry_date', flat=True))
        return {('diary_missing', f'Site diary missing for {d:%a} {d.day} {d:%b}')
                for d in rules.expected_days(now, False, rules.DEFAULT_REGION, start=self.job_start)
                if d not in written}

    def phase7_site_diary(self):
        # A live job with no diary for the last working days: each is on the PM's Needs me, nobody else's
        missing = self._diary_missing()
        badge, items = self.needs('pm')
        self.assertEqual({i for i in items if i[0] == 'diary_missing'}, missing)
        self.assertEqual(badge, len(missing) + 1)  # and this month's application
        self.assertEqual(self.needs('owner')[0], 0)
        self.get('sub', self.pu('diary_register'), status=403)
        self.get('client', self.pu('diary_register'), status=403)

        day = TODAY().isoformat()
        r = self.get('pm', self.pu('diary_edit', day))
        data = form_data(r, 'notes')
        data.update({'weather_am': 'DRY', 'weather_pm': 'RAIN', 'site_start': '07:30', 'site_finish': '17:00',
                     'own_staff_count': '2', 'notes': 'First fix joinery started at the bar.'})
        r = self.post('pm', self.pu('diary_edit', day), data)
        r = self.get('pm', self.pu('diary_line_add', day, 'delays'))
        data = form_data(r, 'hours_lost')
        data.update({'cause': 'MATERIALS', 'hours_lost': '3', 'description': 'Oak veneer panels delivered damaged',
                     'trades_affected': 'Joinery'})
        self.post('pm', self.pu('diary_line_add', day, 'delays'), data)
        self.post('pm', self.pu('diary_photo_upload') + f'?date={day}', {
            'library': SimpleUploadedFile('bar-front.png', png_bytes(), content_type='image/png'),
            'caption': 'Bar front carcass', 'area': 'Bar', 'taken_on': day, 'attach': 'on'})
        page = text(self.get('pm', self.pu('diary_day', day)))
        for seen in ('DY-001', 'First fix joinery started at the bar', 'Oak veneer panels delivered damaged',
                     'Late or wrong materials', 'Bar front carcass'):
            self.assertIn(seen, page)
        page = text(self.get('pm', self.pu('diary_delays')))
        self.assertIn('Oak veneer panels delivered damaged', page)
        self.assertIn('3', page)
        self.assertIn('Bar front carcass', text(self.get('pm', self.pu('diary_gallery'))))
        # Internal unless shared: the client's overview shows no photos
        self.assertNotIn('Bar front carcass', text(self.get('client', self.pu('project_detail'))))
        # Today's entry isn't due yet, so the missing days are unchanged
        self.assertEqual({i for i in self.needs('pm')[1] if i[0] == 'diary_missing'}, missing)

    # 8 -----------------------------------------------------------------

    def phase8_drawing_snag_pin(self):
        r = self.get('pm', self.pu('document_list'))
        data = form_data(r, 'title')
        data.update({'title': 'Ground floor GA plan', 'reference': 'LBF-JFO-ZZ-00-DR-A-1001', 'category': 'DRAWING',
                     'discipline': 'Architectural', 'revision': 'P01', 'suitability': 'S2', 'visibility': 'SHARED',
                     'issue_now': 'on', 'purpose': data.get('purpose') or 'INFORMATION',
                     'file': SimpleUploadedFile('LBF-JFO-ZZ-00-DR-A-1001-P01.pdf', pdf_bytes(2),
                                                content_type='application/pdf')})
        data.pop('shared_with', None)
        r = self.post('pm', self.pu('document_list'), data, follow=True, status=200)
        page = text(r)
        self.assertIn('Ground floor GA plan uploaded', page)
        self.assertIn('TR-001 issued to', page)
        from projects.models import Document
        with org_context(self.org):
            doc = Document.objects.get(project=self.project, title='Ground floor GA plan')
            rev = doc.current
        self.doc, self.rev = doc, rev
        # The sub and the client see it and are asked to acknowledge the transmittal
        for who in ('sub', 'client'):
            page = text(self.get(who, self.pu('document_list')))
            self.assertIn('LBF-JFO-ZZ-00-DR-A-1001', page)
            self.assertIn('TR-001', {ref for _, ref in self.needs(who)[1]})
        # The sub acknowledges
        r = self.get('sub', self.pu('transmittal_list'))
        from documents.models import Transmittal
        with org_context(self.org):
            tr = Transmittal.objects.get(project=self.project)
        self.post('sub', self.pu('transmittal_detail', tr.pk), {'action': 'acknowledge'})
        self.assertNotIn('TR-001', {ref for _, ref in self.needs('sub')[1]})

        # The PM opens the viewer and pins a new snag, assigned to the sub
        viewer = self.pu('drawing_viewer', doc.pk, rev.pk)
        page = self.get('pm', viewer).content.decode()
        self.assertIn('LBF-JFO-ZZ-00-DR-A-1001', page)
        create = self.pu('issue_create') + f'?pin_doc={doc.pk}&pin_rev={rev.pk}&pin_page=2&pin_x=0.42&pin_y=0.61'
        r = self.get('pm', create)
        self.assertIn('LBF-JFO-ZZ-00-DR-A-1001 rev P01', text(r))
        data = form_data(r, 'title')
        data.update({'title': 'Bar front veneer chipped at corner', 'assignee': self.sub_user.pk, 'priority': 'HIGH',
                     'due_date': (TODAY() + timedelta(days=5)).isoformat(), 'description': 'Make good before handover'})
        r = self.post('pm', create, data)
        self.assertIn(viewer.rstrip('/'), r.url)
        from projects.models import Issue
        with org_context(self.org):
            self.snag = Issue.objects.get(project=self.project, title__startswith='Bar front veneer')
        # The sub is told, sees it on their Needs me, and sees the pin on the drawing
        self.assertIn(f'{self.snag.reference}: Bar front veneer chipped at corner',
                      text(self.get('sub', self.u('notifications'))))
        self.assertIn(('snag', self.snag.reference), self.needs('sub')[1])
        page = self.get('sub', viewer).content.decode()
        self.assertIn(self.snag.reference, page)
        self.assertIn('Bar front veneer chipped at corner', page)
        snag_page = text(self.get('sub', self.pu('issue_detail', self.snag.pk)))
        self.assertIn('On drawing', snag_page)
        # The client sees the drawing but not the sub's snag or its pin
        self.assertNotIn(self.snag.reference, self.get('client', viewer).content.decode())
        # The sub marks it ready; it moves to the PM to inspect
        self.post('sub', self.pu('issue_detail', self.snag.pk), {'action': 'ready'})
        self.assertNotIn(('snag', self.snag.reference), self.needs('sub')[1])
        self.assertIn(('snag_inspect', self.snag.reference), self.needs('pm')[1])
        self.post('pm', self.pu('issue_detail', self.snag.pk), {'action': 'close'})
        self.assertNotIn(('snag_inspect', self.snag.reference), self.needs('pm')[1])

    # 9 -----------------------------------------------------------------

    def phase9_rfi_change_variation(self):
        client_user = User.objects.get(email='cara@northerntaverns.test')
        r = self.get('pm', self.pu('rfi_create'))
        data = form_data(r, 'question')
        data.update({'title': 'Bar top material', 'question': 'Spec says oak; the interior design shows marble. Which?',
                     'ball_in_court_user': client_user.pk, 'due_date': (TODAY() + timedelta(days=3)).isoformat(),
                     'is_shared': 'on', 'priority': 'HIGH'})
        r = self.post('pm', self.pu('rfi_create'), data)
        from rfis.models import RFI
        with org_context(self.org):
            rfi = RFI.objects.get(project=self.project)
        self.assertEqual(rfi.ref, 'RFI-001')
        self.assertIn(('rfi', 'RFI-001'), self.needs('client')[1])
        self.assertIn('Bar top material', text(self.get('sub', self.pu('rfi_list'))))  # shared: the sub can read it
        # The client answers it
        self.post('client', self.pu('rfi_detail', rfi.pk), {'action': 'respond', 'response': 'Marble, as the design.'})
        self.assertNotIn(('rfi', 'RFI-001'), self.needs('client')[1])
        page = text(self.get('pm', self.pu('rfi_detail', rfi.pk)))
        self.assertIn('Marble, as the design', page)

        # A change event from the RFI, priced, then the client variation it becomes
        r = self.get('pm', self.pu('change_event_create') + f'?rfi={rfi.pk}')
        data = form_data(r, 'title')
        data.update({'title': 'Marble bar top instead of oak', 'description': 'Supply and fit marble bar top.',
                     'time_impact_days': '2'})
        r = self.post('pm', self.pu('change_event_create') + f'?rfi={rfi.pk}', data)
        ce_url = r.url
        self.post('pm', ce_url, {'action': 'add_line', 'kind': 'COST', 'description': 'Marble from Oak & Ash',
                                 'amount': '4200.00', 'trade': self.joinery.pk, 'package': self.pkg_oak.pk})
        self.post('pm', ce_url, {'action': 'add_line', 'kind': 'REVENUE', 'description': 'Marble bar top',
                                 'amount': '6000.00', 'trade': '', 'package': ''})
        page = text(self.get('pm', ce_url))
        self.assertIn('CE-001', page)
        self.assertIn('RFI-001', page)
        self.assertIn('6,000', page)
        r = self.post('pm', ce_url, {'action': 'raise_variation'})
        vo_url = r.url
        page = text(self.get('pm', vo_url))
        self.assertIn('VO-001', page)
        self.assertIn('6,000', page)
        # Drafts are internal
        self.get('client', vo_url, status=404)
        self.post('pm', vo_url, {'action': 'submit'})
        self.assertIn(('client_variation', 'VO-001'), self.needs('client')[1])
        page = text(self.get('client', vo_url))
        self.assertIn('Marble bar top instead of oak', page)
        self.assertNotIn('CE-001', page)  # no internal refs or costs for the client
        self.assertNotIn('4,200', page)
        r = self.get('client', vo_url)
        data = form_data(r, 'decision')
        data.update({'decision': 'APPROVED', 'approved_value': '6000.00', 'client_reference': 'PO-7781',
                     'decision_notes': 'Approved.'})
        self.post('client', vo_url, data)
        self.assertNotIn(('client_variation', 'VO-001'), self.needs('client')[1])
        page = text(self.get('pm', vo_url))
        self.assertIn('Approved', page)
        self.assertIn('PO-7781', page)
        # Forecast final account on the overview: 250,000 + 6,000
        self.assertIn('256,000', text(self.get('pm', self.pu('contract_detail'))))
        # The answered RFI and the change event wait on the PM until closed out
        self.assertNeeds('pm', {('rfi', 'RFI-001'), ('change', 'CE-001'), self.ap_due()})
        self.post('pm', self.pu('rfi_detail', rfi.pk), {'action': 'close'})
        for step in ('notify', 'quote', 'agree', 'implement'):
            self.post('pm', ce_url, {'action': step})
        self.assertIn('Implemented', text(self.get('pm', ce_url)))
        self.assertNeeds('pm', {self.ap_due()})

    # 10 ----------------------------------------------------------------

    def phase10_application_for_payment(self):
        self.get('sub', self.pu('application_list'), status=403)
        r = self.get('pm', self.pu('application_create'))
        data = form_data(r, 'work_done')
        data.update({'valuation_date': TODAY().isoformat(), 'work_done': '60000.00', 'materials_on_site': '0',
                     'materials_off_site': '0', 'notes': ''})
        r = self.post('pm', self.pu('application_create'), data)
        ap_url = r.url
        page = text(self.get('pm', ap_url))
        # 60,000 work + 6,000 approved VO = 66,000; less 5% retention (JCT IC) = 62,700; VAT 20% = 12,540
        for figure in ('AP-001', 'Draft', '£66,000.00', '−£3,300.00', '£62,700.00', '£12,540.00', '£75,240.00'):
            self.assertIn(figure, page)
        self.get('client', ap_url, status=404)  # a draft is ours
        self.assertNeeds('pm', {('payment', 'AP-001')})  # this month's prompt now points at the draft
        self.post('pm', ap_url, {'action': 'submit'})
        self.assertNeeds('pm', set())
        self.assertIn('Application for payment AP-001 submitted to you',
                      text(self.get('client', self.u('notifications'))))
        self.assertIn(('payment', 'AP-001'), self.needs('client')[1])
        # The client gives a payment notice for the full sum
        r = self.get('client', ap_url)
        page = text(r)
        self.assertIn('£75,240.00', page)
        data = form_data(r, 'action=payment_notice') if 'action=payment_notice' in str(r.content) else \
            {'action': 'payment_notice'}
        data.update({'action': 'payment_notice', 'amount': '62700.00', 'basis': 'As applied.'})
        self.post('client', ap_url, data)
        # Notified: what waits on the client now is paying it (a pay less notice only if paying less)
        self.assertIn(('payment', 'AP-001'), self.needs('client')[1])
        page = text(self.get('pm', ap_url))
        self.assertIn('Cara Client gave a payment notice', text(self.get('pm', self.u('notifications'))))
        self.assertIn('Notified', page)
        # Paid: recorded by the PM
        self.post('pm', ap_url, {'action': 'payment', 'amount': '75240.00', 'received_on': TODAY().isoformat(),
                                 'reference': 'BACS 0042'})
        page = text(self.get('pm', ap_url))
        self.assertIn('Paid', page)
        self.assertIn('BACS 0042', page)
        self.assertNotIn(('payment', 'AP-001'), self.needs('client')[1])
        self.assertNotIn('payment', {k for k, _ in self.needs('pm')[1]})
        # The client sees it paid, and never our costs
        page = text(self.get('client', ap_url))
        self.assertIn('Paid', page)
        self.assertNotIn('48,500', page)
        self.assertNotIn('49,750', page)

    # 11 ----------------------------------------------------------------

    def phase11_sub_payment_and_cost_report(self):
        # The owner records the sub's CIS status (owners and admins only) and the reverse charge
        self.get('pm', self.u('subpay_tax', self.oak.pk), status=403)
        r = self.get('owner', self.u('subpay_tax', self.oak.pk))
        data = form_data(r, 'cis_status')
        data.update({'cis_status': 'REGISTERED', 'verification_number': 'V1234567890', 'verified_on': TODAY().isoformat(),
                     'utr': '1234567890', 'reverse_charge': 'on'})
        self.post('owner', self.u('subpay_tax', self.oak.pk), data)
        # The PM sets up the subcontract's payment terms from the company defaults
        self.post('pm', self.pu('subpay_open', self.pkg_oak.pk))
        self.assertIn(('subpayment', 'Apply for payment: Joinery'), self.needs('sub')[1])
        # The sub applies through the portal: 20,000 gross to date, 5,000 of it materials
        apply_url = self.pu('subpay_apply', self.pkg_oak.pk)
        r = self.get('sub', apply_url)
        data = form_data(r, 'gross_to_date')
        self.assertTrue(data['valuation_date'])
        data.update({'gross_to_date': '20000.00', 'materials': '5000.00', 'notes': 'October valuation'})
        r = self.post('sub', apply_url, data)
        sa_url = r.url
        page = text(self.get('sub', sa_url))
        self.assertIn('SA-001', page)
        self.assertIn('£19,000.00', page)  # 20,000 less 5% retention
        self.assertNotIn('48,500', page)  # nothing of our order or costs
        self.assertNotIn('49,750', page)
        self.assertIn('Oak & Ash Joinery applied for payment: SA-001', text(self.get('pm', self.u('notifications'))))
        self.assertIn(('subpayment', 'SA-001'), self.needs('pm')[1])
        # Our payment notice, then the payment with CIS
        self.post('pm', sa_url, {'action': 'payment_notice', 'amount': '19000.00', 'basis': 'As applied, less retention'})
        r = self.get('pm', sa_url)
        data = form_data(r, 'gross')
        self.assertEqual(Decimal(data['gross']), Decimal('19000.00'))
        data.update({'gross': '19000.00', 'materials': '5000.00', 'paid_on': TODAY().isoformat(), 'reference': 'BACS 0043'})
        r = self.post('pm', sa_url, data, follow=True, status=200)
        page = text(r)
        # CIS at 20% on 19,000 less 5,000 materials = 2,800; reverse charge so no VAT; net 16,200
        self.assertIn('Payment recorded: £19,000.00 gross posted to the cost ledger, CIS £2,800.00, net paid £16,200.00',
                      page)
        self.assertNotIn(('subpayment', 'SA-001'), self.needs('pm')[1])
        self.assertIn('Payment advice for SA-001: £16,200.00 paid to you', text(self.get('sub', self.u('notifications'))))
        page = text(self.get('owner', self.u('subpay_cis')))
        self.assertIn('Oak & Ash Joinery', page)
        self.assertIn('2,800.00', page)

        # The cost report reads only the ledger: check it against the ledger's own totals
        report = self.get('pm', self.pu('cost_report'))
        ledger = self.get('pm', self.pu('cost_ledger'))
        from costs import ledger as ledger_api
        with org_context(self.org):
            totals = ledger_api.totals(self.project)
        budget = sum(t['BUDGET'] for t in totals.values())
        committed = sum(t['COMMITMENT'] for t in totals.values())
        actual = sum(t['ACTUAL'] for t in totals.values())
        self.assertEqual((budget, committed, actual), (Decimal('225000.00'), Decimal('49750.00'), Decimal('19000.00')))
        data = report.context['data']
        t = data['total']
        self.assertEqual((t.budget, t.committed, t.actual), (budget, committed, actual))
        # Forecast per code is the larger of budget, committed and actual: joinery 50,000 (over 49,750 committed) + 175,000
        self.assertEqual(data['final_cost'], Decimal('225000.00'))
        self.assertEqual(data['final_account'], Decimal('256000.00'))
        self.assertEqual(data['margin'], Decimal('31000.00'))
        # The CSV the QS takes away says the same
        rows = list(csv.reader(io.StringIO(self.get('pm', self.pu('cost_report') + '?export=csv').content.decode())))
        total_row = next(r for r in rows if len(r) > 1 and r[1] == 'Total')
        self.assertEqual([Decimal(x) for x in total_row[3:6]], [budget, committed, actual])
        self.assertEqual(Decimal(next(r for r in rows if len(r) > 1 and r[1] == 'Forecast final account')[3]),
                         Decimal('256000.00'))
        page = text(report)
        for figure in ('225,000', '49,750', '19,000'):
            self.assertIn(figure, page)
        self.assertIn('19,000', text(ledger))
        # Commercial position: 250,000 + approved 6,000 = 256,000, the same on the contract page and the report
        self.assertIn('256,000', page)
        self.assertIn('256,000', text(self.get('pm', self.pu('contract_detail'))))
        # Nobody outside sees the cost report
        for who in ('sub', 'client', 'est'):
            self.assertIn(self.c[who].get(self.pu('cost_report')).status_code, (403, 404))
        # Another company sees none of it, even with the URLs
        for name in ('project_detail', 'cost_report', 'needs_me'):
            url = self.pu(name) if name != 'needs_me' else self.u(name)
            self.assertEqual(self.c['rival'].get(url).status_code, 404, url)
        r = self.c['rival'].get(reverse('needs_me', args=[self.other_org.slug]))
        self.assertEqual(r.context['total_count'], 0)

        # The month's CVR reconciles to the same figures: value from AP-001 (66,000 gross before retention),
        # cost from the ledger (19,000), final account 256,000 and final cost 225,000 as the cost report says
        r = self.get('pm', self.pu('cvr_list'))
        now = r.context['now']
        self.assertEqual((now.value, now.cost, now.margin), (Decimal('66000.00'), Decimal('19000.00'),
                                                             Decimal('47000.00')))
        self.assertEqual((now.final_account, now.final_cost, now.forecast_margin),
                         (data['final_account'], data['final_cost'], data['margin']))
        self.assertEqual((now.certified_to_date, now.paid_to_date), (Decimal('62700.00'), Decimal('62700.00')))
        self.post('pm', self.pu('cvr_list'), {'month': TODAY().replace(day=1).isoformat()})
        self.assertIn('CVR-001 Being prepared £66,000 £19,000 £47,000', text(self.get('pm', self.pu('cvr_list'))))
        # Cashflow: 62,700 received ex VAT in, 19,000 out on the ledger, both this month
        r = self.get('pm', self.pu('cvr_cashflow'))
        month = next(m for m in r.context['data']['months'] if m.is_current)
        self.assertEqual((month.in_actual, month.out_actual), (Decimal('62700.00'), Decimal('19000.00')))
        for who in ('sub', 'client', 'est'):
            self.assertIn(self.c[who].get(self.pu('cvr_list')).status_code, (403, 404))

    # ================================================================== Release 3

    # 12 ----------------------------------------------------------------

    def phase12_inspection_fails_and_raises_a_snag(self):
        from projects.models import Issue
        from quality.models import Inspection
        for who in ('sub', 'client'):  # Quality is staff only
            self.get(who, self.pu('inspection_list'), status=403)
        # The fire doors checklist from the company's starting library, on the joiner's work, inspected by the PM
        r = self.get('pm', self.pu('inspection_create'))
        data = form_data(r, 'template')
        template = next(t for t in r.context['form'].fields['template'].queryset if t.name == 'Fire doors')
        self.assertEqual(int(data['ball_in_court_user']), self.pm.pk)  # whoever starts it, unless they change it
        data.update({'template': template.pk, 'area': 'Bar store', 'package': self.pkg_oak.pk})
        r = self.post('pm', self.pu('inspection_create'), data)
        with org_context(self.org):
            ins = Inspection.objects.get(project=self.project)
            checks = list(ins.items.order_by('order'))
        self.assertEqual(r.url, ins.get_absolute_url())
        self.assertEqual((ins.ref, ins.title, len(checks)), ('INS-001', 'Fire doors, Bar store', 8))
        self.assertNeeds('pm', {('inspection', 'INS-001')})
        # Six pass, the self-closer fails (with a note and a photo), the glazing is N/A
        failed = None
        for check in checks:
            body = {'result': 'PASS', 'note': ''}
            if check.text.startswith('Self-closer'):
                failed = check
                body = {'result': 'FAIL', 'note': 'Closer stalls 100 mm open',
                        'photo': SimpleUploadedFile('closer.png', png_bytes(), content_type='image/png')}
            elif check.text.startswith('Glazing'):
                body['result'] = 'NA'
            self.post('pm', self.pu('inspection_item', ins.pk, check.pk), body)
        # Finishing records the outcome from the answers
        r = self.post('pm', self.pu('inspection_action', ins.pk), {'action': 'finish', 'notes': ''}, follow=True,
                      status=200)
        self.assertIn('INS-001 failed (6 pass, 1 fail, 1 N/A)', text(r))
        self.assertNeeds('pm', set())  # finished: no longer waiting on the inspector
        # One tap raises a snag from the failed check, for the joiner whose work it is, due in a week
        r = self.post('pm', self.pu('inspection_snag', ins.pk, failed.pk), follow=True, status=200)
        with org_context(self.org):
            snag = Issue.objects.get(project=self.project, title__startswith='Failed check')
        due = TODAY() + timedelta(days=7)
        self.assertIn(f'{snag.reference} raised and assigned to Sid Oak, due {due:%a} {due.day} {due:%b}', text(r))
        self.assertEqual((snag.title, snag.assignee, snag.location, snag.issue_type, snag.due_date),
                         ('Failed check: Self-closer shuts the door fully from any angle', self.sub_user, 'Bar store',
                          'QUALITY', due))
        self.assertIn('Closer stalls 100 mm open', snag.description)
        self.assertTrue(snag.photo)  # the check's photo goes with it
        # A second tap doesn't raise a second snag
        self.post('pm', self.pu('inspection_snag', ins.pk, failed.pk))
        with org_context(self.org):
            self.assertEqual(Issue.objects.filter(project=self.project, title__startswith='Failed check').count(), 1)
        self.assertNeeds('sub', {('snag', snag.reference)})
        self.assertNeeds('pm', set())
        # The joiner puts it right; the PM inspects and closes it
        self.post('sub', self.pu('issue_detail', snag.pk), {'action': 'ready'})
        self.assertNeeds('sub', set())
        self.assertNeeds('pm', {('snag_inspect', snag.reference)})
        self.post('pm', self.pu('issue_detail', snag.pk), {'action': 'close'})
        self.assertNeeds('pm', set())
        page = text(self.get('pm', self.pu('inspection_list') + '?status=failed'))
        self.assertIn('INS-001', page)
        self.assertIn('Fire doors, Bar store', page)

    # 13 ----------------------------------------------------------------

    def phase13_hot_works_permit(self):
        from quality import rules as quality_rules
        from quality.models import Permit
        self.get('sub', self.pu('permit_list'), status=403)
        # The PM asks for a hot works permit; the form starts at the next quarter hour, for the company's usual 8 hours
        r = self.get('pm', self.pu('permit_create') + '?type=HOT_WORKS')
        data = form_data(r, 'title')
        data.update({'permit_type': 'HOT_WORKS', 'title': 'Cut and weld steel bracket for the bar canopy',
                     'location': 'Bar, north wall', 'package': '', 'holder': 'Site welder', 'method_notes': ''})
        r = self.post('pm', self.pu('permit_create'), data, follow=True, status=200)
        with org_context(self.org):
            permit = Permit.objects.get(project=self.project)
        # The PM asked, so the PM can't approve it: it goes to the owner, the company's admin
        self.assertIn('PTW-001 sent to Olive Owner to approve. Work must not start until it is approved.', text(r))
        self.assertEqual((permit.ref, permit.ball_in_court_user, permit.expires_at - permit.starts_at),
                         ('PTW-001', self.owner, timedelta(hours=8)))
        self.assertNeeds('owner', {('permit', 'PTW-001')})
        self.assertNeeds('pm', set())
        self.assertIn('PTW-001: Hot works permit to approve', text(self.get('owner', self.u('notifications'))))
        act = self.pu('permit_action', permit.pk)
        every = quality_rules.precautions_for('HOT_WORKS')
        self.post('pm', act, {'action': 'approve', 'precautions': every, 'extra': ''}, status=403)  # who asked
        with org_context(self.org):
            permit.refresh_from_db()
        self.assertEqual(permit.status, 'REQUESTED')
        # The owner approves it, but only once every precaution is ticked
        r = self.post('owner', act, {'action': 'approve', 'precautions': every[:-1], 'extra': ''}, follow=True,
                      status=200)
        self.assertIn('Tick every precaution once it is in place (1 not ticked)', text(r))
        r = self.post('owner', act, {'action': 'approve', 'precautions': every, 'extra': ''}, follow=True, status=200)
        self.assertIn('PTW-001 approved.', text(r))
        with org_context(self.org):
            permit.refresh_from_db()
        self.assertEqual((permit.status, permit.approved_by, permit.ball_in_court_user),
                         ('APPROVED', self.owner, self.pm))
        self.assertNeeds('owner', set())
        # Back with the PM, who asked for it: on their Needs me from the day it runs out until it is closed
        runs_out = timezone.localdate(permit.expires_at)
        self.assertNeeds('pm', {('permit', 'PTW-001')} if runs_out <= TODAY() else set())
        self.assertIn('PTW-001: Hot works permit approved', text(self.get('pm', self.u('notifications'))))
        page = text(self.get('pm', self.pu('permit_print', permit.pk)))
        self.assertIn('PTW-001', page)
        self.assertIn('Cut and weld steel bracket for the bar canopy', page)
        # Closing hot works needs the area left safe and the fire watch done
        self.post('pm', act, {'action': 'close', 'safe': 'on', 'note': ''})
        with org_context(self.org):
            permit.refresh_from_db()
        self.assertEqual(permit.status, 'APPROVED')
        self.post('pm', act, {'action': 'close', 'safe': 'on', 'fire_watch': 'on', 'note': 'Bracket fixed'})
        with org_context(self.org):
            permit.refresh_from_db()
        self.assertEqual((permit.status, permit.closed_by, permit.fire_watch_minutes), ('CLOSED', self.pm, 60))
        self.assertNeeds('pm', set())

    # 14 ----------------------------------------------------------------

    def phase14_fire_stopping(self):
        from quality.models import Penetration
        # The owner records a seal on the joiner's package; the photo of the finished seal marks it sealed
        r = self.get('owner', self.pu('penetration_create'))
        data = form_data(r, 'location')
        data.update({'location': 'L0, bar store, east wall', 'element': 'WALL', 'fire_rating': '60', 'service': 'PIPE_PLASTIC',
                     'size': '110 mm pipe', 'system': 'Intumescent pipe collar', 'detail_reference': 'IPC-110',
                     'package': self.pkg_oak.pk, 'installer': 'Sid Oak', 'installed_on': '', 'notes': '',
                     'before_photo': SimpleUploadedFile('before.png', png_bytes(), content_type='image/png'),
                     'after_photo': SimpleUploadedFile('after.png', png_bytes(), content_type='image/png')})
        r = self.post('owner', self.pu('penetration_create'), data, follow=True, status=200)
        self.assertIn('FS-001 added and marked sealed, ready to inspect.', text(r))
        with org_context(self.org):
            self.seal = Penetration.objects.get(project=self.project)
        self.assertEqual((self.seal.status, self.seal.installed_on, self.seal.ball_in_court_user),
                         ('SEALED', TODAY(), self.pm))
        # Waiting on the PM to inspect it, not on the owner who recorded it
        self.assertNeeds('pm', {('firestopping', 'FS-001')})
        self.assertNeeds('owner', set())
        self.post('pm', self.pu('penetration_action', self.seal.pk), {'action': 'sign_off', 'note': 'Collar checked'})
        with org_context(self.org):
            self.seal.refresh_from_db()
        self.assertEqual((self.seal.status, self.seal.inspected_by), ('SIGNED_OFF', self.pm))
        self.assertNeeds('pm', set())
        # The register's CSV, which the handover pack carries, has it signed off
        rows = list(csv.reader(io.StringIO(
            self.get('pm', self.pu('penetration_list') + '?export=csv').content.decode())))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], 'FS-001')
        self.assertIn('Intumescent pipe collar', rows[1])
        self.assertIn('Signed off', rows[1])

    # 15 ----------------------------------------------------------------

    def phase15_incident_and_visitor_sign_in(self):
        from safety.models import Incident, SiteAttendance
        # The owner reports a near miss on a phone; it goes to the job's PM to investigate
        r = self.get('owner', self.pu('incident_report'))
        data = form_data(r, 'description')
        self.assertTrue(data['occurred_at'])  # defaults to now
        data.update({'description': 'Board slid off the trolley by the goods lift. Nobody hurt.',
                     'location': 'Goods lift lobby', 'anyone_hurt': 'False'})
        r = self.post('owner', self.pu('incident_report'), data)
        with org_context(self.org):
            inc = Incident.objects.get(project=self.project)
        self.assertEqual(r.url, inc.get_absolute_url())
        self.assertIn(f'Thank you. {inc.ref} is recorded and the project manager has been told.',
                      text(self.get('owner', r.url)))
        self.assertEqual((inc.ref, inc.kind, inc.ball_in_court_user, inc.due_date),
                         ('INC-001', 'NEAR_MISS', self.pm, TODAY() + timedelta(days=7)))
        self.assertNeeds('pm', {('incident', 'INC-001')})
        self.assertNeeds('owner', set())
        for who in ('sub', 'client'):
            self.get(who, self.pu('incident_list'), status=403)
        # The PM investigates and gives the owner an action
        self.post('pm', self.pu('incident_transition', inc.pk), {'transition': 'investigate'})
        r = self.get('pm', self.pu('incident_action_add', inc.pk))
        data = form_data(r, 'description')
        data.update({'description': 'Buy a trolley with side rails', 'owner': self.owner.pk,
                     'due_date': (TODAY() + timedelta(days=3)).isoformat()})
        self.post('pm', self.pu('incident_action_add', inc.pk), data)
        self.assertNeeds('owner', {('safety', 'INC-001')})
        self.assertNeeds('pm', {('incident', 'INC-001')})
        # Closing needs the findings written and no open actions
        r = self.post('pm', self.pu('incident_transition', inc.pk), {'transition': 'close'}, follow=True, status=200)
        self.assertIn('Write up the findings and lessons learned. Finish the open actions', text(r))
        with org_context(self.org):
            action = inc.actions.get()
        self.post('owner', self.pu('incident_action_update', inc.pk, action.pk), {'do': 'done'})
        self.assertNeeds('owner', set())
        r = self.get('pm', self.pu('incident_edit', inc.pk))
        data = form_data(r, 'findings')
        data.update({'findings': 'Loose boards on a flat trolley. Use the railed trolley.', 'root_cause': 'Wrong trolley'})
        self.post('pm', self.pu('incident_edit', inc.pk), data)
        r = self.post('pm', self.pu('incident_transition', inc.pk), {'transition': 'close'}, follow=True, status=200)
        self.assertIn('INC-001 closed.', text(r))
        self.assertNeeds('pm', set())
        self.assertIn('INC-001', text(self.get('owner', self.u('company_incidents'))))

        # A visitor scans the QR poster: no account, just a name and company, and nothing else about the job
        poster = text(self.get('pm', self.pu('safety_poster')))
        signin = re.search(r'/sign-in/[\w-]+/', poster).group(0)
        visitor = Client()
        r = visitor.get(signin)
        self.assertEqual(r.status_code, 200)
        page = text(r)
        self.assertIn('Leeds Bar Fit-out', page)
        for private in ('Journey Fitout', 'Northern Taverns', 'Call Lane', 'Oak & Ash'):
            self.assertNotIn(private, page)
        r = visitor.post(signin, {'name': 'Vic Visitor', 'company': 'Building Control'}, follow=True)
        self.assertIn('Signed in at', text(r))
        page = text(self.get('pm', self.pu('safety_on_site')))
        self.assertIn('Vic Visitor', page)
        self.assertIn('Building Control', page)
        # Only the visitor's own phone signs them out
        Client().post(signin + 'out/')
        with org_context(self.org):
            self.assertIsNone(SiteAttendance.objects.get(project=self.project, name='Vic Visitor').signed_out_at)
        visitor.post(signin + 'out/')
        with org_context(self.org):
            row = SiteAttendance.objects.get(project=self.project, name='Vic Visitor')
        self.assertIsNotNone(row.signed_out_at)
        self.assertEqual(row.kind, SiteAttendance.KIND_VISITOR)
        self.assertNotIn('Vic Visitor', text(self.get('pm', self.pu('safety_on_site'))))
        self.assertIn('Vic Visitor', text(self.get('pm', self.pu('safety_sign_in_log'))))
        self.assertEqual(self.c['rival'].get(self.pu('safety_on_site')).status_code, 404)

    # 16 ----------------------------------------------------------------

    def _mspdi(self, joinery_finish_days):
        """The job's programme as MS Project (or Asta) saves it as XML, with dates from this week."""
        monday = TODAY() - timedelta(days=TODAY().weekday())

        def at(d, h=8):
            return f'{d.isoformat()}T{h:02d}:00:00'

        def task(uid, name, start, finish, *, summary=0, milestone=0, after=None, done=0):
            link = (f'<PredecessorLink><PredecessorUID>{after}</PredecessorUID><Type>1</Type></PredecessorLink>'
                    if after else '')
            return (f'<Task><UID>{uid}</UID><ID>{uid}</ID><Name>{name}</Name><OutlineLevel>{1 if summary else 2}'
                    f'</OutlineLevel><Summary>{summary}</Summary><Milestone>{milestone}</Milestone>'
                    f'<Start>{at(start)}</Start><Finish>{at(finish, 17)}</Finish>'
                    f'<PercentComplete>{done}</PercentComplete>{link}</Task>')
        end = monday + timedelta(days=60)
        tasks = [
            task(1, 'Leeds Bar Fit-out', monday, end, summary=1),
            task(10, 'Bar joinery install', TODAY(), TODAY() + timedelta(days=joinery_finish_days)),
            task(11, 'Suspended ceilings', TODAY() + timedelta(days=3), TODAY() + timedelta(days=8), after=10),
            task(12, 'Decorations', monday + timedelta(days=21), monday + timedelta(days=25), after=11),
            task(13, 'Practical completion', end, end, milestone=1, after=12),
        ]
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Project xmlns="http://schemas.microsoft.com/project"><Name>Leeds bar.xml</Name>'
                f'<Tasks>{"".join(tasks)}</Tasks>'
                '<Resources><Resource><UID>1</UID><Name>Oak &amp; Ash Joinery</Name></Resource>'
                '<Resource><UID>2</UID><Name>Pennine Drylining</Name></Resource></Resources>'
                '<Assignments><Assignment><UID>1</UID><TaskUID>10</TaskUID><ResourceUID>1</ResourceUID></Assignment>'
                '<Assignment><UID>2</UID><TaskUID>11</TaskUID><ResourceUID>2</ResourceUID></Assignment>'
                '</Assignments></Project>').encode()

    def _import_programme(self, data):
        from projects.models import ProgrammeImport
        r = self.post('pm', self.pu('programme_import'),
                      {'file': SimpleUploadedFile('Leeds bar.xml', data, content_type='text/xml')})
        with org_context(self.org):
            imp = ProgrammeImport.objects.filter(project=self.project).latest('pk')
        self.assertEqual(r.url, self.pu('programme_import_preview', imp.pk))
        return imp

    def phase16_programme_import_and_look_ahead(self):
        from projects.models import Activity
        for who in ('sub', 'client'):
            self.get(who, self.pu('programme_import'), status=403)
        imp = self._import_programme(self._mspdi(5))
        # Nothing changes until the PM confirms the preview
        with org_context(self.org):
            self.assertFalse(Activity.objects.filter(project=self.project).exists())
        page = text(self.get('pm', self.pu('programme_import_preview', imp.pk)))
        for name in ('Bar joinery install', 'Suspended ceilings', 'Decorations', 'Practical completion'):
            self.assertIn(name, page)
        r = self.post('pm', self.pu('programme_import_preview', imp.pk), {'action': 'apply'}, follow=True,
                      status=200)
        self.assertIn('Programme updated from Leeds bar.xml: 5 added, 0 changed, 0 removed.', text(r))
        with org_context(self.org):
            lines = {a.source_uid: a for a in Activity.objects.filter(project=self.project).select_related(
                'subcontractor', 'after')}
        self.assertEqual(set(lines), {1, 10, 11, 12, 13})
        # The resource named like the appointed joiner sets the subcontractor; the other is just "who"
        self.assertEqual((lines[10].subcontractor, lines[10].trade), (self.oak, self.joinery))
        self.assertEqual((lines[11].subcontractor, lines[11].responsible), (None, 'Pennine Drylining'))
        self.assertEqual(lines[12].after, lines[11])
        # Re-importing the updated file moves the joinery by its MS Project ID: changed, not duplicated
        imp = self._import_programme(self._mspdi(7))
        r = self.post('pm', self.pu('programme_import_preview', imp.pk), {'action': 'apply'}, follow=True,
                      status=200)
        self.assertIn('Programme updated from Leeds bar.xml: 0 added, 1 changed, 0 removed.', text(r))
        with org_context(self.org):
            self.assertEqual(Activity.objects.filter(project=self.project).count(), 5)
            self.assertEqual(Activity.objects.get(project=self.project, source_uid=10).finish,
                             TODAY() + timedelta(days=7))

        def lookahead(who, weeks):
            r = self.get(who, self.pu('programme_lookahead') + f'?weeks={weeks}')
            return [ln.activity.name for ln in r.context['rows']], r
        # Two and three weeks from this Monday hold the joinery and the ceilings; four weeks add the decorations.
        # The summary line and the completion milestone eight weeks out are never in it.
        self.assertEqual(lookahead('pm', 2)[0], ['Bar joinery install', 'Suspended ceilings'])
        self.assertEqual(lookahead('pm', 3)[0], ['Bar joinery install', 'Suspended ceilings'])
        self.assertEqual(lookahead('pm', 4)[0], ['Bar joinery install', 'Suspended ceilings', 'Decorations'])
        # Filtered to the joiner
        r = self.get('pm', self.pu('programme_lookahead') + f'?weeks=4&sub={self.oak.pk}')
        self.assertEqual([ln.activity.name for ln in r.context['rows']], ['Bar joinery install'])
        csv_rows = list(csv.reader(io.StringIO(
            self.get('pm', self.pu('programme_lookahead') + '?weeks=4&export=csv').content.decode())))
        self.assertEqual([row[0] for row in csv_rows[1:]], ['Bar joinery install', 'Suspended ceilings', 'Decorations'])
        # The joiner sees only their own line; the client sees every shared line but never the CSV
        names, r = lookahead('sub', 4)
        self.assertEqual(names, ['Bar joinery install'])
        self.assertNotIn('Pennine Drylining', text(r))
        self.assertEqual(lookahead('client', 4)[0], ['Bar joinery install', 'Suspended ceilings', 'Decorations'])
        self.assertNotIn('text/csv', self.get('client', self.pu('programme_lookahead') + '?export=csv')
                         .get('Content-Type', ''))

    # 17 ----------------------------------------------------------------

    def phase17_meeting_issued_and_actions_done(self):
        from meetings.models import Meeting, MeetingAction
        client_user = User.objects.get(email='cara@northerntaverns.test')
        r = self.get('pm', self.pu('meeting_create'))
        data = form_data(r, 'held_on')
        self.assertEqual(data['held_on'], TODAY().isoformat())
        data.update({'kind': 'PROGRESS', 'starts_at': '10:00', 'location': 'Site office',
                     'attendees': [self.pm.pk, self.sub_user.pk, client_user.pk], 'others': ''})
        r = self.post('pm', self.pu('meeting_create'), data)
        with org_context(self.org):
            meeting = Meeting.objects.get(project=self.project)
            item = meeting.items.order_by('pk').first()
        self.assertEqual(r.url, meeting.get_absolute_url())
        self.assertEqual(meeting.ref, 'MTG-001')
        self.assertNeeds('pm', {('meeting', 'MTG-001')})  # the chair, until the minutes are issued
        # Minutes, then an action for the joiner and one for the PM
        url = meeting.get_absolute_url()
        self.post('pm', url, {'action': 'minutes', 'version': meeting.version, f'item-{item.pk}': 'All on programme.',
                              'notes': ''})
        due = TODAY() + timedelta(days=5)
        for owner, title in ((self.sub_user, 'Send the revised bar front shop drawing'),
                             (self.pm, 'Confirm the marble template date')):
            self.post('pm', url, {'action': 'add_action', 'act-title': title, 'act-owner': owner.pk,
                                  'act-owner_name': '', 'act-due_date': due.isoformat(), 'act-item': ''})
        with org_context(self.org):
            sub_action = MeetingAction.objects.get(meeting=meeting, owner=self.sub_user)
            pm_action = MeetingAction.objects.get(meeting=meeting, owner=self.pm)
        # Not on anyone's Needs me until the minutes are issued
        self.assertNeeds('sub', set())
        self.assertNeeds('pm', {('meeting', 'MTG-001')})
        mail.outbox.clear()
        r = self.post('pm', url, {'action': 'issue'}, follow=True, status=200)
        self.assertIn('MTG-001 minutes issued. 2 people were emailed a link to them', text(r))
        sent = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(sent, ['cara@northerntaverns.test', 'sid@oakash.test'])
        for m in mail.outbox:  # a link, never an attachment
            self.assertFalse(m.attachments)
            self.assertIn(f'/projects/{self.pk}/meetings/', m.body)
        self.assertNeeds('pm', {('meetingaction', pm_action.ref)})
        self.assertNeeds('sub', {('meeting_action', sub_action.ref)})
        self.assertNeeds('client', {('transmittal', 'TR-001')})
        # The client reads the issued minutes but has no actions; the joiner marks theirs done in the portal
        self.assertIn('All on programme.', text(self.get('client', url)))
        self.post('sub', self.pu('meeting_action_update', sub_action.pk), {'status': 'DONE', 'update': 'Sent rev 03'})
        self.assertNeeds('sub', set())
        self.post('pm', self.pu('meeting_action_update', pm_action.pk), {'status': 'DONE', 'update': ''})
        self.assertNeeds('pm', set())
        with org_context(self.org):
            self.assertEqual(set(MeetingAction.objects.filter(meeting=meeting).values_list('status', flat=True)),
                             {'DONE'})
        # Done actions are not carried forward to the next meeting
        r = self.post('pm', self.pu('meeting_create'), {**data, 'held_on': (TODAY() + timedelta(days=7)).isoformat()},
                      follow=True, status=200)
        self.assertIn('MTG-002 set up with the standard agenda.', text(r))
        with org_context(self.org):
            nxt = Meeting.objects.get(project=self.project, ref='MTG-002')
            self.assertEqual(nxt.brought_forward.count(), 0)
        self.post('pm', nxt.get_absolute_url(), {'action': 'cancel', 'note': 'Not needed this week'})
        self.assertNeeds('pm', set())

    # 18 ----------------------------------------------------------------

    def phase18_practical_completion_defects_om_and_handover(self):
        from closeout.models import HandoverPack, OMSection
        from procurement.models import Submittal
        from projects.models import Issue
        client_user = User.objects.get(email='cara@northerntaverns.test')
        # Practical completion on the whole works: the defects period (12 months, the default) runs from it
        r = self.get('pm', self.pu('closeout_section', 'whole'))
        data = form_data(r, 'pc_date')
        data.update({'pc_date': TODAY().isoformat(), 'made_good_on': ''})
        r = self.post('pm', self.pu('closeout_section', 'whole'), data, follow=True, status=200)
        page = text(r)
        # JCT IC's rectification period is 6 months unless the contract says otherwise (advisory)
        from closeout.rules import add_months
        self.assertIn(f'The defects period ends on {add_months(TODAY(), 12):%d %b %Y}.', page)
        self.assertIn('advisory', page.lower())

        # The client reports a defect in the portal: it goes to the PM to give to someone
        r = self.get('client', self.pu('closeout_client'))
        data = form_data(r, 'title')
        data.update({'title': 'WC 2 door does not close', 'location': 'Ground floor WC 2', 'description': ''})
        r = self.post('client', self.pu('closeout_client'), data)
        with org_context(self.org):
            defect = Issue.objects.get(project=self.project, issue_type='DEFECT')
        self.assertIn(f'Thank you. {defect.reference} has been sent to Journey Fitout.',
                      text(self.get('client', r.url)))
        self.assertEqual(defect.created_by, client_user)
        self.assertNeeds('pm', {('defect_assign', defect.reference)})
        # The PM gives it to the joiner; they fix it; the PM checks it
        r = self.get('pm', self.pu('issue_edit', defect.pk))
        data = form_data(r, 'title')
        data.update({'assignee': self.sub_user.pk, 'due_date': (TODAY() + timedelta(days=5)).isoformat()})
        self.post('pm', self.pu('issue_edit', defect.pk), data)
        self.assertNeeds('pm', set())
        self.assertNeeds('sub', {('snag', defect.reference)})
        page = text(self.get('client', self.pu('closeout_client')))
        self.assertIn('WC 2 door does not close', page)
        self.assertIn('Being fixed', page)
        self.post('sub', self.pu('issue_detail', defect.pk), {'action': 'ready'})
        self.assertNeeds('pm', {('snag_inspect', defect.reference)})
        self.post('pm', self.pu('issue_detail', defect.pk), {'action': 'close'})
        self.assertNeeds('pm', set())
        self.assertIn('Fixed and checked', text(self.get('client', self.pu('closeout_client'))))

        # O&M manual: sections from the company template; the joinery section asked of the joiner
        self.post('pm', self.pu('closeout_om'), {'action': 'setup'})
        with org_context(self.org):
            sections = list(OMSection.objects.filter(project=self.project).order_by('order'))
            joinery = next(s for s in sections if 'joinery' in s.title.lower())
            others = [s for s in sections if s.pk != joinery.pk]
        for s in others:  # not on this job
            self.post('pm', self.pu('closeout_om_section', s.pk), {'title': s.title, 'package': '',
                                                                  'contents': s.contents, 'not_needed': 'on'})
        self.post('pm', self.pu('closeout_om_section', joinery.pk), {'title': joinery.title, 'package': self.pkg_oak.pk,
                                                                    'contents': joinery.contents})
        r = self.get('pm', self.pu('closeout_om_request', joinery.pk))
        data = form_data(r, 'due_date')
        self.post('pm', self.pu('closeout_om_request', joinery.pk), {**data, 'note': 'Care of the oak veneer'})
        with org_context(self.org):
            om_sub = Submittal.objects.get(project=self.project, kind=Submittal.OM)
        self.assertNeeds('sub', {('submittal', om_sub.ref)})
        self.post('sub', om_sub.get_absolute_url(), {
            'action': 'submit', 'note': 'Joinery O&M',
            'files': SimpleUploadedFile('joinery-om.pdf', pdf_bytes(3), content_type='application/pdf')})
        self.assertNeeds('sub', set())
        self.assertNeeds('pm', {('submittal', om_sub.ref)})
        self.post('pm', om_sub.get_absolute_url(), {'action': 'review', 'code': 'A', 'comments': ''})
        self.assertNeeds('pm', set())
        r = self.post('pm', self.pu('closeout_om'), {'action': 'compile'}, follow=True, status=200)
        self.assertIn('O&M manual version 1 is ready to download.', text(r))
        comp = r.context['compilations'][0]
        self.post('pm', self.pu('closeout_om'), {'action': 'share', 'compilation': comp.pk})
        self.assertEqual(self.get('client', self.pu('closeout_om_download', comp.pk)).status_code, 200)
        self.get('sub', self.pu('closeout_om_download', comp.pk), status=403)

        # The golden thread handover pack: the drawing at its current revision, the fire-stopping register and
        # the O&M, each with its checksum
        r = self.post('pm', self.pu('closeout_packs'), {'note': 'Handover at practical completion'}, follow=True,
                      status=200)
        with org_context(self.org):
            pack = HandoverPack.objects.get(project=self.project)
            items = list(pack.items.order_by('pk'))
        self.assertIn(f'{pack} built: check it, then issue it to the client.', text(r))
        self.assertEqual(pack.status, HandoverPack.READY)
        self.assertEqual({(i.category, i.reference) for i in items if i.category != 'OM'},
                         {('DRAWING', 'LBF-JFO-ZZ-00-DR-A-1001'), ('FIRE_STOPPING', '')})
        self.assertTrue(all(len(i.sha256) == 64 for i in items))
        register = next(i for i in items if i.category == 'FIRE_STOPPING')
        self.assertEqual(register.title, 'Fire-stopping register (1 rows)')
        self.get('client', pack.get_absolute_url(), status=403)  # not until it is issued
        self.post('pm', pack.get_absolute_url(), {'action': 'issue'})
        page = text(self.get('client', self.pu('closeout_client')))
        self.assertIn('Fire-stopping register (1 rows)', page)
        r = self.c['client'].get(self.pu('closeout_pack_item', pack.pk, register.pk))
        self.assertEqual(r.status_code, 200)
        body = b''.join(r.streaming_content) if r.streaming else r.content
        self.assertEqual(hashlib.sha256(body).hexdigest(), register.sha256)
        rows = list(csv.reader(io.StringIO(body.decode('utf-8-sig'))))
        self.assertEqual([row[0] for row in rows[1:]], ['FS-001'])
        self.assertIn('Signed off', rows[1])
        # The client acknowledges the drawing issue as they take the handover
        from documents.models import Transmittal
        with org_context(self.org):
            tr = Transmittal.objects.get(project=self.project)
        self.post('client', self.pu('transmittal_detail', tr.pk), {'action': 'acknowledge'})
        self.assertNeeds('client', set())

    # 19 ----------------------------------------------------------------

    def phase19_daywork_to_the_change_register(self):
        from changes.models import ChangeEvent
        from cvr.models import DayworkSheet
        self.get('sub', self.pu('daywork_list'), status=403)
        self.get('client', self.pu('daywork_list'), status=403)
        # The contract's daywork rates and percentage additions (Commercial > Dayworks > Rates)
        rates = self.pu('daywork_rates')
        self.post('pm', rates, {'action': 'add_rate', 'rate-kind': 'LABOUR', 'rate-name': 'Joiner', 'rate-unit': 'hour',
                                'rate-rate': '42.50'})
        self.post('pm', rates, {'action': 'terms', 'terms-labour_pct': '80', 'terms-materials_pct': '15',
                                'terms-plant_pct': '10', 'terms-basis': 'Contract Particulars'})
        # On a phone: only what the work was; the date is today
        r = self.post('pm', self.pu('daywork_create'), {'title': 'Make good bar back after marble template',
                                                        'work_date': TODAY().isoformat()})
        with org_context(self.org):
            sheet = DayworkSheet.objects.get(project=self.project)
            joiner = self.project.daywork_rates.get(name='Joiner')
        self.assertEqual((r.url, sheet.reference), (sheet.get_absolute_url(), 'DW-001'))
        for line in ({'line-kind': 'LABOUR', 'line-rate': joiner.pk, 'line-quantity': '8'},
                     {'line-kind': 'MATERIALS', 'line-description': 'Oak lipping', 'line-quantity': '2',
                      'line-unit': 'lengths', 'line-unit_rate': '18.50'},
                     {'line-kind': 'PLANT', 'line-description': 'Tower scaffold', 'line-quantity': '1',
                      'line-unit_rate': '45.00'}):
            self.post('pm', sheet.get_absolute_url(), {'action': 'add_line', **line})
        # 8 h at 42.50 = 340.00 + 80% = 612.00; 2 x 18.50 = 37.00 + 15% = 42.55; 45.00 + 10% = 49.50: 704.05
        page = text(self.get('pm', sheet.get_absolute_url()))
        self.assertIn('£704.05', page)
        # The client's representative signs on the PM's phone
        r = self.post('pm', self.pu('daywork_sign', sheet.pk), {
            'name': 'Cara Client', 'role': 'CLIENT', 'confirm': 'on',
            'signature': SimpleUploadedFile('signature.png', signature_png(), content_type='image/png')})
        self.assertEqual(r.url, sheet.get_absolute_url())
        self.assertNeeds('pm', {('daywork', 'DW-001')})  # signed: to go on the change register within a week
        # One step onto the change register: a new change event priced from the sheet
        r = self.post('pm', sheet.get_absolute_url(), {'action': 'send', 'send-change_event': ''})
        with org_context(self.org):
            ce = ChangeEvent.objects.get(project=self.project, number=2)
            sheet.refresh_from_db()
            ce_lines = {(ln.kind, ln.amount) for ln in ce.lines.all()}
        self.assertEqual(r.url, ce.get_absolute_url())
        self.assertEqual((ce.reference, sheet.status, sheet.change_event_id), ('CE-002', 'IN_CHANGE', ce.pk))
        self.assertEqual(ce_lines, {('REVENUE', Decimal('704.05')), ('COST', Decimal('422.00'))})
        self.assertIn('Signed on the day by Cara Client', ce.description)
        self.assertNeeds('pm', {('change', 'CE-002')})
        # It becomes the client's variation VO-002, which they approve at the daywork value
        r = self.post('pm', ce.get_absolute_url(), {'action': 'raise_variation'})
        vo_url = r.url
        self.post('pm', vo_url, {'action': 'submit'})
        self.assertIn(('client_variation', 'VO-002'), self.needs('client')[1])
        data = form_data(self.get('client', vo_url), 'decision')
        data.update({'decision': 'APPROVED', 'approved_value': '704.05', 'client_reference': 'PO-7790',
                     'decision_notes': 'Dayworks as signed.'})
        self.post('client', vo_url, data)
        for step in ('notify', 'quote', 'agree', 'implement'):
            self.post('pm', ce.get_absolute_url(), {'action': step})
        self.assertNeeds('pm', set())
        self.assertNeeds('client', set())
        # The forecast final account: 250,000 + 6,000 (VO-001) + 704.05 (VO-002)
        self.assertIn('Forecast final account £256,704', text(self.get('pm', self.pu('contract_detail'))))

    # 21 ----------------------------------------------------------------

    def phase21_month_end_cvr_signed_off_and_cashflow(self):
        """The month ends: the CVR started in phase 11 is finished, signed off, and agrees with the cashflow."""
        from cvr.models import CVRPeriod
        month = TODAY().replace(day=1)
        self.next_month = (month + timedelta(days=32)).replace(day=1)
        # Before the month ends the CVR can't go for sign-off
        with org_context(self.org):
            period = CVRPeriod.objects.get(project=self.project)
        url = self.pu('cvr_period', period.pk)
        r = self.post('pm', url, {'action': 'submit'}, follow=True, status=200)
        self.assertIn('has not ended yet', text(r))
        self.travel_to(self.next_month)
        valuation = self.next_month.replace(day=min(self.valuation_day, 28))
        expect_pm = {('cvr', 'CVR-001')}  # Prepare the CVR for last month
        if valuation <= TODAY() + timedelta(days=7):
            expect_pm.add(('payment', f'Application for payment for {valuation.day} {valuation:%b %Y}'))
        self.assertNeeds('pm', expect_pm)
        self.assertNeeds('owner', set())
        data = form_data(self.get('sub', self.pu('subpay_apply', self.pkg_oak.pk)), 'gross_to_date')
        sub_valuation = date.fromisoformat(data['valuation_date'])
        expect_sub = {('subpayment', 'Apply for payment: Joinery')} if sub_valuation <= TODAY() + timedelta(days=7) \
            else set()
        self.assertNeeds('sub', expect_sub)
        self.month_end_needs = {'pm': expect_pm - {('cvr', 'CVR-001')}, 'sub': expect_sub}
        # The PM submits it: its figures are frozen as they stand at the month end
        self.post('pm', url, {'action': 'submit'})
        expect_pm.discard(('cvr', 'CVR-001'))
        self.assertNeeds('pm', expect_pm)
        self.assertNeeds('owner', {('cvr', 'CVR-001')})  # owners and admins sign off
        self.post('pm', url, {'action': 'sign_off'}, status=403)
        r = self.post('owner', url, {'action': 'sign_off'}, follow=True, status=200)
        with org_context(self.org):
            period.refresh_from_db()
        self.assertEqual((period.status, period.signed_off_by), ('SIGNED_OFF', self.owner))
        self.assertNeeds('owner', set())
        # Signed off at the month's figures: value 66,000 (AP-001), cost 19,000 (the ledger), margin 47,000;
        # forecast final account 256,704.05 and final cost 225,000 (the cost report)
        r = self.get('pm', self.pu('cvr_list'))
        page = text(r)
        self.assertIn(f'{month:%B %Y} CVR-001 Signed off by Olive Owner £66,000 £19,000 £47,000 71.2% £256,704 '
                      '£225,000 £31,704 12.4%', page)
        report = self.get('pm', self.pu('cost_report')).context['data']
        self.assertEqual((report['final_account'], report['final_cost']), (Decimal('256704.05'), Decimal('225000.00')))
        # The figures were frozen when it was submitted, and the page shows the same
        self.assertEqual((period.value_to_date, period.cost_to_date, period.contract_sum, period.approved_variations),
                         (Decimal('66000.00'), Decimal('19000.00'), Decimal('250000.00'), Decimal('6704.05')))
        f = self.get('pm', url).context['f']
        self.assertEqual((f.value, f.cost, f.margin), (Decimal('66000.00'), Decimal('19000.00'), Decimal('47000.00')))
        self.assertEqual((f.final_account, f.final_cost), (report['final_account'], report['final_cost']))
        # A signed-off month is locked: no more adjustments
        r = self.post('pm', url, {'action': 'adjust', 'adj-kind': 'ACCRUAL', 'adj-amount': '100.00',
                                  'adj-reason': 'Late invoice'}, follow=True, status=200)
        with org_context(self.org):
            self.assertFalse(period.adjustments.exists())
        # The cashflow ties out: everything in adds up to the forecast final account and everything out to the
        # forecast final cost, received and spent to date as recorded
        data = self.get('pm', self.pu('cvr_cashflow')).context['data']
        t = data['totals']
        self.assertEqual((t['in_actual'], t['out_actual']), (Decimal('62700.00'), Decimal('19000.00')))
        self.assertEqual(t['in'], Decimal('256704.05'))
        self.assertEqual(t['in'], data['final_account'])
        self.assertEqual(t['out'], Decimal('225000.00'))
        self.assertEqual(t['out'], data['final_cost'])
        self.assertEqual(t['net'], Decimal('31704.05'))
        for who in ('sub', 'client', 'est'):
            self.assertIn(self.c[who].get(self.pu('cvr_cashflow')).status_code, (403, 404))

    # 20 ----------------------------------------------------------------

    def phase20_portal_markup_api_and_reports(self):
        """Release 3 wave 2: the subcontractor's and client's portal additions, drawing mark-up, the API, reports."""
        import json

        from cvr.models import DayworkSheet
        from quality.models import Penetration
        from safety.models import Incident

        # The joiner records fire-stopping on their own package in the portal; the PM inspects it
        r = self.get('sub', self.pu('portal_firestop_create'))
        data = form_data(r, 'location')
        self.assertEqual(int(data['package']), self.pkg_oak.pk)  # their only package
        data.update({'location': 'L0, back bar, west wall', 'element': 'WALL', 'fire_rating': '30', 'service': 'CABLE',
                     'size': 'Cable bundle', 'system': 'Fire batt and mastic', 'detail_reference': '',
                     'installer': 'Sid Oak', 'installed_on': '', 'notes': '',
                     'before_photo': SimpleUploadedFile('b.png', png_bytes(), content_type='image/png'),
                     'after_photo': SimpleUploadedFile('a.png', png_bytes(), content_type='image/png')})
        self.post('sub', self.pu('portal_firestop_create'), data)
        with org_context(self.org):
            seal = Penetration.objects.get(project=self.project, location='L0, back bar, west wall')
        self.assertEqual((seal.ref, seal.status, seal.sealed_by, seal.ball_in_court_user),
                         ('FS-002', 'SEALED', self.sub_user, self.pm))
        self.assertNeeds('pm', {('firestopping', 'FS-002')})
        self.get('sub2', self.pu('portal_firestop'), status=404)  # the losing bidder has no part in the job
        self.get('client', self.pu('portal_firestop'), status=403)
        # Failed: it goes on the joiner's Needs me to put right, and stays on the PM's to chase (who recorded it
        # has no login on our side)
        self.post('pm', self.pu('penetration_action', seal.pk), {'action': 'fail', 'note': 'Mastic not tooled in'})
        self.assertNeeds('pm', {('firestopping', 'FS-002')})
        self.assertNeeds('sub', {('firestopping_sub', 'FS-002')})
        detail = self.pu('portal_firestop_detail', seal.pk)
        self.post('sub', self.pu('portal_firestop_photo', seal.pk), {
            'stage': 'AFTER', 'photo': SimpleUploadedFile('a2.png', png_bytes(), content_type='image/png')})
        r = self.post('sub', self.pu('portal_firestop_action', seal.pk), {'action': 'reseal'})
        self.assertEqual(r.url, detail)
        self.assertNeeds('sub', set())
        self.assertNeeds('pm', {('firestopping', 'FS-002')})
        r = self.post('pm', self.pu('penetration_action', seal.pk), {'action': 'sign_off', 'note': ''}, follow=True,
                      status=200)
        self.assertIn('FS-002 signed off.', text(r))
        with org_context(self.org):
            seal.refresh_from_db()
        self.assertEqual((seal.status, seal.inspected_by), ('SIGNED_OFF', self.pm))
        self.assertNeeds('pm', set())

        # The joiner reports a near miss from the portal: an internal incident with the PM
        r = self.get('sub', self.pu('portal_near_miss'))
        data = form_data(r, 'description')
        data.update({'description': 'Offcut fell from the scaffold tower. Nobody below.', 'location': 'Bar',
                     'anyone_hurt': 'False'})
        self.post('sub', self.pu('portal_near_miss'), data)
        with org_context(self.org):
            inc = Incident.objects.get(project=self.project, number=2)
        self.assertEqual((inc.ref, inc.created_by, inc.ball_in_court_user), ('INC-002', self.sub_user, self.pm))
        self.assertNeeds('pm', {('incident', 'INC-002')})
        self.assertNeeds('sub', set())
        self.get('sub', inc.get_absolute_url(), status=403)  # never the investigation
        r = self.get('pm', self.pu('incident_edit', inc.pk))
        data = form_data(r, 'findings')
        data.update({'findings': 'Toe boards missing on the tower. Fitted.'})
        self.post('pm', self.pu('incident_edit', inc.pk), data)
        self.post('pm', self.pu('incident_transition', inc.pk), {'transition': 'close'})
        self.assertNeeds('pm', set())

        # A daywork sheet the client signs in their own portal instead of on the PM's phone
        self.post('pm', self.pu('daywork_create'), {'title': 'Extra blocking for the TV brackets',
                                                    'work_date': TODAY().isoformat()})
        with org_context(self.org):
            sheet = DayworkSheet.objects.get(project=self.project, number=2)
            joiner = self.project.daywork_rates.get(name='Joiner')
        self.post('pm', sheet.get_absolute_url(), {'action': 'add_line', 'line-kind': 'LABOUR', 'line-rate': joiner.pk,
                                                   'line-quantity': '2'})
        client_user = User.objects.get(email='cara@northerntaverns.test')
        self.post('pm', sheet.get_absolute_url(), {'action': 'ask_client', 'signer': client_user.pk})
        self.assertIn(('daywork_sign', 'DW-002'), self.needs('client')[1])
        portal_page = self.pu('daywork_portal_sign', sheet.pk)
        page = text(self.get('client', portal_page))
        self.assertIn('Joiner', page)
        self.assertNotIn('42.50', page)  # the hours, never our rates or money
        self.assertNotIn('153.00', page)
        data = form_data(self.get('client', portal_page), 'name')
        data.update({'name': 'Cara Client', 'confirm': 'on',
                     'signature': SimpleUploadedFile('signature.png', signature_png(), content_type='image/png')})
        self.post('client', portal_page, data)
        with org_context(self.org):
            sheet.refresh_from_db()
        self.assertEqual((sheet.status, sheet.signed_by_name), ('SIGNED', 'Cara Client'))
        self.assertNeeds('client', set())
        self.assertNeeds('pm', {('daywork', 'DW-002')})
        # 2 h at 42.50 = 85.00 + 80% = 153.00: onto the register and agreed as a change event
        r = self.post('pm', sheet.get_absolute_url(), {'action': 'send', 'send-change_event': ''})
        ce_url = r.url
        from changes.models import ChangeEvent
        with org_context(self.org):
            ce = ChangeEvent.objects.get(project=self.project, number=3)
            self.assertEqual({(ln.kind, ln.amount) for ln in ce.lines.all()},
                             {('REVENUE', Decimal('153.00')), ('COST', Decimal('85.00'))})
        self.assertEqual(ce_url, ce.get_absolute_url())
        self.assertIn('CE-003 raised from DW-002', text(self.get('pm', ce_url)))
        for step in ('notify', 'quote', 'agree', 'implement'):
            self.post('pm', ce_url, {'action': step})
        self.assertNeeds('pm', set())

        # Drawing mark-up: the PM's layer on the GA is private until shared with the team
        layer_data = json.dumps({'v': 1, 'shapes': [{'t': 'cloud', 'p': 1, 'c': 'red', 'w': 3, 'a': [0.4, 0.5],
                                                       'b': [0.6, 0.7]}]})
        r = self.c['pm'].post(self.pu('markup_save', self.doc.pk, self.rev.pk), {'data': layer_data, 'version': ''})
        self.assertEqual(r.status_code, 200, r.content[:300])
        layer_id = r.json()['id']
        self.assertEqual(self.c['sub'].get(self.pu('markup_data', self.doc.pk, layer_id)).status_code, 404)
        r = self.c['pm'].post(self.pu('markup_share', self.doc.pk, layer_id), {'share': 'TEAM'})
        self.assertIn(r.status_code, (200, 302))
        self.assertEqual(self.c['owner'].get(self.pu('markup_data', self.doc.pk, layer_id)).status_code, 200)
        self.assertEqual(self.c['sub'].get(self.pu('markup_data', self.doc.pk, layer_id)).status_code, 404)

        # The read-only API: the owner makes a token; it reads this company's job and nothing of another's
        r = self.post('owner', self.u('api_settings'), {'name': 'Power BI', 'scopes': ['registers', 'commercial'],
                                                       'expires': '365'}, status=200)
        token = r.context['text']
        api = Client()
        rows = api.get(reverse('api_projects'), HTTP_AUTHORIZATION=f'Bearer {token}').json()['results']
        self.assertEqual([(row['id'], row['name']) for row in rows], [(self.pk, 'Leeds Bar Fit-out')])
        rival_token = self.post('rival', reverse('api_settings', args=[self.other_org.slug]),
                                {'name': 'Theirs', 'scopes': [], 'expires': '365'}, status=200).context['text']
        self.assertEqual(api.get(reverse('api_projects_detail', args=[self.pk]),
                                 HTTP_AUTHORIZATION=f'Bearer {rival_token}').status_code, 404)
        self.assertEqual(api.get(reverse('api_projects')).status_code, 401)

        # Company reports: the portfolio margin agrees with the job's CVR and cost report
        r = self.get('owner', self.u('report_margin'))
        row = next(x for x in r.context['rows'] if x['project'].pk == self.pk)
        self.assertEqual((row['f'].final_account, row['f'].final_cost), (Decimal('256704.05'), Decimal('225000.00')))
        for who in ('pm', 'sub', 'client'):
            self.assertIn(self.c[who].get(self.u('report_margin')).status_code, (403, 404))
