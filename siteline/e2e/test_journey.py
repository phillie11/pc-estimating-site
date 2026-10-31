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
  12 Needs me counts for each role at each step
"""
import csv
import io
import re
from html.parser import HTMLParser
from datetime import timedelta
from decimal import Decimal

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


@override_settings(EMAIL_SYNC=True)
class JourneyTest(TestCase):
    """Phases share state, so they run in order inside one test."""

    # ------------------------------------------------------------------ set-up (ORM)

    def setUp(self):
        self.owner = User.objects.create_user(email='owner@journey.test', password=PW, full_name='Olive Owner')
        self.est = User.objects.create_user(email='est@journey.test', password=PW, full_name='Ezra Estimator')
        self.pm = User.objects.create_user(email='pm@journey.test', password=PW, full_name='Priya Manager')
        self.org = create_organization('Journey Fitout', self.owner)
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
        self.assertEqual(got, set(expected), f'Needs me for {who}')
        return badge

    # ------------------------------------------------------------------ the journey

    def test_one_job_through_every_module(self):
        phases = [self.phase1_lead_to_tender, self.phase2_subs_quote, self.phase3_levelling_and_submit,
                  self.phase4_won_handover_appoint, self.phase5_contract_budget_order, self.phase6_procurement,
                  self.phase7_site_diary, self.phase8_drawing_snag_pin, self.phase9_rfi_change_variation,
                  self.phase10_application_for_payment, self.phase11_sub_payment_and_cost_report]
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
        self.assertEqual(out, expected, f'Needs me after phase {n}')

    # Needs me for every role once each phase has finished (diary days aside, checked by count above).
    # Everything else is cleared by the phase that raised it, and is asserted there when it was waiting.
    EXPECTED_NEEDS = {
        8: {'client': {('transmittal', 'TR-001')}},  # the client hasn't acknowledged the drawing issue
        9: {'client': {('transmittal', 'TR-001')}},
        10: {'client': {('transmittal', 'TR-001')}},
        11: {'client': {('transmittal', 'TR-001')}},
    }

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
        start = TODAY() - timedelta(days=30)
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
        data['valuation_day'] = str(TODAY().day)
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
        now = timezone.localtime().replace(tzinfo=None)
        start = TODAY() - timedelta(days=30)
        return {('diary_missing', f'Site diary missing for {d:%a} {d.day} {d:%b}')
                for d in rules.expected_days(now, False, rules.DEFAULT_REGION, start=start)}

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
