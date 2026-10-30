"""
Create a demo workspace with realistic data.

    python manage.py seed_demo            # creates 'northbank-interiors'
    python manage.py seed_demo --reset    # deletes and recreates it

Logins (password for all: DemoPass!2026):
    owner@demo.test        Owner: sees estimating and projects, and switches between them
    estimator@demo.test    Staff, estimating only: bids, sub tenders, pipeline, analytics
    pm@demo.test           Staff, projects only: live jobs, procurement, RAMS, variations
    client@demo.test       External: client on the Leeds bar project
    sub@demo.test          External: joinery subcontractor (appointed on Leeds, pricing Kingsway)
    bids@demo.test         External: drylining subcontractor pricing Kingsway
"""
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from accounts.views import create_organization
from directory.models import Package, Subcontractor, Trade
from projects.models import Activity, Document, DocumentRevision, Issue, Project, ProjectMember, Task
from rfis.models import RFI
from tenancy.context import org_context
from tenancy.models import Membership, Notification, Organization, log_activity
from tenders.models import Addendum, AddendumFile, AddendumReceipt, TenderQuery
from delivery.models import Rams, SubVariation
from variations.models import Variation

PASSWORD = 'DemoPass!2026'
SLUG = 'northbank-interiors'


def user(email, name, title=''):
    u, created = User.objects.get_or_create(email=email, defaults={'full_name': name, 'job_title': title})
    if created:
        u.set_password(PASSWORD)
        u.save()
    return u


def demo_pdf(title, subtitle=''):
    """A real one-page PDF showing the document's title, so demo downloads open properly."""
    def esc(t):
        t = t.replace('\u2013', '-').replace('\u2014', '-').replace('\u2018', "'").replace('\u2019', "'")
        return t.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)').encode('latin-1', 'replace').decode('latin-1')
    text = (f'BT /F1 22 Tf 60 760 Td ({esc(title)}) Tj ET\n'
            f'BT /F1 12 Tf 60 735 Td ({esc(subtitle)}) Tj ET\n'
            f'BT /F1 10 Tf 60 60 Td (Demo document created by seed_demo) Tj ET\n').encode('latin-1')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R '
        b'/Resources << /Font << /F1 5 0 R >> >> >>',
        b'<< /Length ' + str(len(text)).encode() + b' >>\nstream\n' + text + b'endstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out, offsets = bytearray(b'%PDF-1.4\n'), []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
    out += b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    out += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return bytes(out)


class Command(BaseCommand):
    help = 'Create a demo workspace'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true')

    @transaction.atomic
    def handle(self, *args, reset=False, **opts):
        existing = Organization.objects.filter(slug=SLUG).first()
        if existing and not reset:
            self.stdout.write('Demo workspace already exists (use --reset).')
            return
        if existing:
            existing.delete()

        owner = user('owner@demo.test', 'Sam Whitaker', 'Managing Director')
        pm = user('pm@demo.test', 'Priya Natarajan', 'Project Manager')
        est = user('estimator@demo.test', 'Tom Reilly', 'Senior Estimator')
        client = user('client@demo.test', 'Hannah Cole', 'Development Manager')
        subu = user('sub@demo.test', 'Gary Lund', 'Contracts Manager')
        bidu = user('bids@demo.test', 'Mark Dyson', 'Estimator')

        org = create_organization('Northbank Interiors', owner)
        org.slug = SLUG
        org.save()
        Membership.objects.create(organization=org, user=pm, role=Membership.ROLE_MEMBER, area=Membership.AREA_DELIVERY)
        Membership.objects.create(organization=org, user=est, role=Membership.ROLE_MEMBER, area=Membership.AREA_ESTIMATING)
        Membership.objects.create(organization=org, user=client, role=Membership.ROLE_EXTERNAL)
        Membership.objects.create(organization=org, user=subu, role=Membership.ROLE_EXTERNAL)
        Membership.objects.create(organization=org, user=bidu, role=Membership.ROLE_EXTERNAL)

        now, today = timezone.now(), timezone.localdate()
        with org_context(org):
            t = {x.name: x for x in Trade.objects.all()}

            def sub(name, trades, status='APPROVED', rating=4, pl_days=200, el_days=220, **kw):
                s = Subcontractor.objects.create(
                    company_name=name, status=status, rating=rating,
                    public_liability_expiry=today + timedelta(days=pl_days),
                    employers_liability_expiry=today + timedelta(days=el_days), **kw)
                s.trades.set([t[n] for n in trades])
                return s

            s_join = sub('Lund Joinery Ltd', ['Joinery'], contact_name='Gary Lund', email='sub@demo.test',
                         phone='0161 555 0101', regions='North West, Yorkshire', accreditations='CHAS, FSC CoC')
            s_dry = sub('Pennine Drylining', ['Drylining & partitions', 'Suspended ceilings'], rating=5, pl_days=21,
                        contact_name='Mark Dyson', email='bids@demo.test', regions='North West')
            s_elec = sub('Brightside Electrical', ['Electrical'], contact_name='Aisha Khan', email='aisha@brightside.example',
                         regions='National', accreditations='NICEIC', pl_days=-5)
            s_floor = sub('Mersey Flooring Co', ['Flooring'], rating=3, contact_name='Dave Price', regions='North West')
            sub('Apex Mechanical', ['Mechanical', 'Plumbing'], status='PENDING', rating=None, contact_name='Lee Grant')
            sub('Crest Decorators', ['Decorating'], contact_name='Jo Hart', pl_days=12, regions='Midlands')
            sub('Rapid Strip Out', ['Strip out & demolition'], status='STRUCK_OFF', rating=2,
                notes='Struck off after H&S breach on site, March.')

            def project(name, stage, value, prob=None, client_name='', addr='', due_days=None, owner_u=est, **kw):
                p = Project.objects.create(name=name, stage=stage, value=Decimal(value), probability=prob,
                                           client_name=client_name, site_address=addr, owner=owner_u,
                                           tender_due=(now + timedelta(days=due_days)).replace(hour=12, minute=0)
                                           if due_days is not None else None, created_by=owner, **kw)
                return p

            leeds = project('Bar & kitchen fit-out, Call Lane', Project.STAGE_LIVE, '1240000', client_name='Neon Leisure Group',
                            addr='48 Call Lane, Leeds LS1', sector='HOSPITALITY', owner_u=est, manager=pm,
                            handed_over_at=now - timedelta(days=50), handed_over_by=est,
                            handover_notes='Tender qualified: no out-of-hours working, client to provide asbestos R&D survey.\n'
                                           'Neighbouring units trading throughout; agree noisy works windows with centre management.\n'
                                           'Joinery priced on Lund Joinery quote (lowest by 8%); bar back veneer still to be confirmed.',
                            start_date=today - timedelta(days=28), completion_date=today + timedelta(days=45),
                            description='Full strip-out and fit-out of ground floor bar and first floor kitchen. Live neighbouring units.')
            project('Showroom refurbishment, Shrewsbury', Project.STAGE_WON, '2150000', client_name='Westway Motor Group',
                    addr='Battlefield Rd, Shrewsbury', sector='AUTOMOTIVE', start_date=today + timedelta(days=40),
                    completion_date=today + timedelta(days=150))
            for name, stage, value, client_name, addr, sector, start, finish in [
                ('Retail unit, Liverpool ONE', Project.STAGE_LIVE, '480000', 'Kumo Retail', 'Liverpool ONE', 'RETAIL', -60, 20),
                ('Office CAT A, Salford Quays', Project.STAGE_LIVE, '1650000', 'Arden Property', 'Salford Quays', 'OFFICE', -14, 84),
                ('Pub refurbishment, York', Project.STAGE_WON, '390000', 'Ouse Inns', 'Micklegate, York', 'HOSPITALITY', 21, 77),
            ]:
                project(name, stage, value, client_name=client_name, addr=addr, sector=sector, owner_u=est, manager=pm,
                        start_date=today + timedelta(days=start), completion_date=today + timedelta(days=finish))
            project('Jewellery store, Argyle Arcade', Project.STAGE_TENDERING, '640000', 60, 'Halden & Co',
                    'Argyle Arcade, Glasgow', 3, sector='RETAIL', lead_source='Framework')
            project('Office CAT B, Kingsway', Project.STAGE_TENDERING, '1850000', 35, 'Arden Property', 'Kingsway, London WC2',
                    9, sector='OFFICE')
            project('Gym fit-out, Ellesmere Port', Project.STAGE_LEAD, '420000', 20, 'Pulse Fitness', 'Ellesmere Port', 21,
                    sector='HOSPITALITY', lead_source='Repeat client')
            project('Aparthotel refurb, Hammersmith', Project.STAGE_SUBMITTED, '3100000', 40, 'Thackeray Living',
                    'Hammersmith, London W6', -6, sector='HOSPITALITY')
            project('Store refit, Trafford Centre', Project.STAGE_SUBMITTED, '780000', 55, 'Kumo Retail', 'Trafford Centre', -12,
                    sector='RETAIL')
            project('Restaurant fit-out, Spinningfields', Project.STAGE_TENDERING, '560000', 45, 'Saltbox Dining',
                    'Spinningfields, Manchester', 16, sector='HOSPITALITY', owner_u=pm, lead_source='Referral')
            project('Clinic refurbishment, Altrincham', Project.STAGE_TENDERING, '310000', 50, 'Cedar Health',
                    'Altrincham', 27, sector='HEALTHCARE', lead_source='Website')
            # Tender milestones, in days from today:
            # received, site visit, queries close, sub quotes, programme, quality, logistics
            for name, days in {
                'Jewellery store, Argyle Arcade': (-18, -10, -4, 0, -2, 1, 2),
                'Office CAT B, Kingsway': (-12, -5, 2, 5, 4, 6, 7),
                'Gym fit-out, Ellesmere Port': (-2, 6, 14, 16, 12, None, None),
                'Aparthotel refurb, Hammersmith': (-40, -30, -16, -12, -14, -9, -8),
                'Store refit, Trafford Centre': (-35, -25, -20, -16, -18, None, -14),
                'Restaurant fit-out, Spinningfields': (-5, 3, 9, 11, 10, 13, 14),
                'Clinic refurbishment, Altrincham': (0, 8, 18, 21, 19, 24, None),
            }.items():
                fields = ['tender_received', 'site_visit', 'queries_close', 'quotes_due', 'programme_due',
                          'quality_due', 'logistics_due']
                Project.objects.filter(name=name).update(**{
                    f: today + timedelta(days=d) for f, d in zip(fields, days) if d is not None})
            lost = project('College refurbishment, Pershore', Project.STAGE_LOST, '960000', 30, 'Vale Education', 'Pershore', -40,
                           sector='EDUCATION', outcome_notes='2nd of 5. 4.1% above preferred bidder.')
            lost.decided_at = today - timedelta(days=30)
            lost.save()
            for i, (n, v) in enumerate([('Unit 12 retail fit-out', '510000'), ('Dealer refurb, Blackburn', '1320000'),
                                        ('Restaurant, Covent Garden', '890000')]):
                p = project(n, Project.STAGE_COMPLETE, v, client_name='Various')
                p.decided_at = today - timedelta(days=60 + i * 70)
                p.save()
            p = project('Sports bar, Elveden', Project.STAGE_LOST, '700000', client_name='Holiday Parks UK')
            p.decided_at = today - timedelta(days=120)
            p.save()
            # A year of results so tender analytics has something to show
            for n, stage, v, sector, client_name, source, days, notes in [
                ('Café chain rollout, phase 2', 'COMPLETE', '185000', 'HOSPITALITY', 'Kettle & Crumb', 'Repeat client', 200, ''),
                ('Dental practice, Chester', 'LOST', '96000', 'HEALTHCARE', 'Bright Smile Group', 'Website', 150,
                 'Client went with a local contractor.'),
                ('Flagship store, King Street', 'COMPLETE', '1450000', 'RETAIL', 'Kumo Retail', 'Framework', 290, ''),
                ('Office refurb, Spinningfields', 'LOST', '2300000', 'OFFICE', 'Arden Property', 'Framework', 95,
                 'Programme 6 weeks longer than the winning bid.'),
                ('Hotel bedrooms, Harrogate', 'LOST', '1100000', 'HOSPITALITY', 'Thackeray Living', 'Referral', 210, ''),
                ('Primary school toilets, Wirral', 'DECLINED', '140000', 'EDUCATION', 'Wirral Council', 'Portal', 75, ''),
                ('Barbers, Northern Quarter', 'COMPLETE', '42000', 'RETAIL', 'Fade Co', 'Website', 330, ''),
                ('Car showroom, Preston', 'LOST', '880000', 'AUTOMOTIVE', 'Westway Motor Group', 'Repeat client', 255,
                 'Lost on price, 2.3% higher.'),
            ]:
                p = project(n, stage, v, client_name=client_name, sector=sector, lead_source=source,
                            owner_u=pm if sector == 'RETAIL' else est, outcome_notes=notes)
                p.decided_at = today - timedelta(days=days)
                p.save()
            # Bids were logged weeks before they were decided; created_at is auto-set, so backdate it directly
            for i, p in enumerate(Project.objects.filter(decided_at__isnull=False)):
                Project.objects.filter(pk=p.pk).update(created_at=now - timedelta(days=(today - p.decided_at).days + 28 + i % 4 * 9))

            # Leeds live project detail
            ProjectMember.objects.create(project=leeds, user=pm, role=ProjectMember.ROLE_PM, added_by=owner)
            ProjectMember.objects.create(project=leeds, user=client, role=ProjectMember.ROLE_CLIENT,
                                         company_name='Neon Leisure Group', added_by=pm)
            ProjectMember.objects.create(project=leeds, user=subu, role=ProjectMember.ROLE_SUBCONTRACTOR,
                                         company_name='Lund Joinery Ltd', subcontractor=s_join, added_by=pm)

            Package.objects.create(project=leeds, subcontractor=s_join, trade=t['Joinery'], status='APPOINTED',
                                   quote_value=Decimal('186500'))
            Package.objects.create(project=leeds, subcontractor=s_dry, trade=t['Drylining & partitions'], status='APPOINTED',
                                   quote_value=Decimal('94200'))
            Package.objects.create(project=leeds, subcontractor=s_elec, trade=t['Electrical'], status='QUOTED',
                                   quote_value=Decimal('132000'))
            Package.objects.create(project=leeds, subcontractor=s_floor, trade=t['Flooring'], status='ENQUIRY')

            r1 = RFI.objects.create(project=leeds, title='Bar back panelling – veneer species', category='SPECIFICATION',
                                    question='Drawing 1203-JD-04 calls for "dark oak veneer" but the finishes schedule lists '
                                             'smoked eucalyptus. Please confirm which is correct before we order.',
                                    created_by=subu, ball_in_court_user=client, due_date=today - timedelta(days=2),
                                    document_reference='1203-JD-04 rev C', priority='HIGH')
            RFI.objects.create(project=leeds, title='Kitchen extract route through first floor slab', category='DESIGN',
                               question='Proposed extract duct clashes with a downstand beam on grid C/4. Can the route '
                                        'move 600mm east?', created_by=pm, ball_in_court_user=client, due_date=today + timedelta(days=4))
            r3 = RFI.objects.create(project=leeds, title='Fire door ironmongery set', category='TECHNICAL',
                                    question='Confirm ironmongery set for doors D07–D12.', created_by=pm, ball_in_court_user=pm,
                                    due_date=today - timedelta(days=10), status=RFI.STATUS_RESPONDED,
                                    response='Use set FD30-B as per the schedule issued 3 Oct.', responded_by=client,
                                    responded_at=now - timedelta(days=8))
            RFI.objects.create(project=leeds, title='Internal: confirm crane permit date', category='SITE', is_shared=False,
                               question='Check with council when the lifting permit lands.', created_by=pm, ball_in_court_user=pm,
                               due_date=today + timedelta(days=1))

            Variation.objects.create(project=leeds, title='Additional acoustic ceiling to private dining', cause='CLIENT_INSTRUCTION',
                                     description='Client instruction on site walk 2 Oct: upgrade private dining ceiling to '
                                                 '40dB acoustic MF ceiling with plasterboard finish.',
                                     value=Decimal('18450'), time_impact_days=2, status=Variation.STATUS_SUBMITTED,
                                     raised_by=pm, submitted_at=now - timedelta(days=3))
            Variation.objects.create(project=leeds, title='Replace rotten joists found during strip-out', cause='SITE_CONDITION',
                                     description='14 no. joists in rear store found rotten. Replace like for like.',
                                     value=Decimal('6200'), approved_value=Decimal('5900'), status=Variation.STATUS_APPROVED,
                                     raised_by=pm, submitted_at=now - timedelta(days=20), decided_at=now - timedelta(days=16),
                                     decided_by=client, client_reference='NLG-PO-4471')
            Variation.objects.create(project=leeds, title='Bar back veneer change (if eucalyptus)', cause='SPEC_CHANGE', rfi=r1,
                                     description='Price difference if smoked eucalyptus is confirmed.', value=Decimal('7300'),
                                     raised_by=pm, proceed_at_risk=False)

            # Leeds programme: started four weeks ago, 73 days on site
            def act(name, start, days, progress=0, after=None, who='', milestone=False, shared=True):
                s = today + timedelta(days=start)
                return Activity.objects.create(project=leeds, name=name, start=s, finish=s if milestone else s + timedelta(days=days - 1),
                                               progress=progress, after=after, responsible=who, is_milestone=milestone,
                                               is_shared=shared, created_by=pm)
            a0 = act('Site possession', -28, 0, 100, milestone=True)
            a1 = act('Strip out & soft demolition', -28, 10, 100, a0, 'Northbank labour')
            a2 = act('First fix M&E', -17, 16, 85, a1, 'Brightside Electrical')
            a3 = act('Drylining & ceilings', -12, 24, 55, a1, 'Pennine Drylining')
            act('Building control first fix inspection', 3, 0, 0, a2, milestone=True)
            act('Kitchen extract & ductwork', -6, 12, 30, a1, 'Apex Mechanical')
            a5 = act('Bar joinery manufacture', -10, 28, 40, None, 'Lund Joinery')
            act('Bar joinery install', 19, 12, 0, a5, 'Lund Joinery')
            act('Second fix M&E', 12, 16, 0, a3, 'Brightside Electrical')
            a8 = act('Flooring', 22, 10, 0, a3, 'Mersey Flooring')
            a9 = act('Decoration', 32, 6, 0, a8, 'Crest Decorators')
            act('Snagging & clean', 38, 6, 0, a9, 'Northbank labour')
            act('Contingency (internal)', 38, 7, 0, a9, shared=False)
            act('Practical completion', 45, 0, 0, a9, milestone=True)

            Task.objects.create(project=leeds, title='Issue joinery shop drawings for approval', assignee=subu,
                                due_date=today + timedelta(days=3), priority='HIGH', created_by=pm)
            Task.objects.create(project=leeds, title='Book building control first fix inspection', assignee=pm,
                                due_date=today - timedelta(days=1), created_by=pm)
            Task.objects.create(project=leeds, title='Update programme after M&E coordination', assignee=pm,
                                due_date=today + timedelta(days=6), created_by=pm)
            # Live-job portal: Lund Joinery's RAMS and variations on Leeds
            join_pkg = Package.objects.get(project=leeds, subcontractor=s_join)
            for title, scope, status, days, comments in [
                ('Bar joinery installation', 'Bar back, front and shelving', Rams.APPROVED, 20, ''),
                ('Working at height: mobile towers', 'Bulkhead and high-level joinery', Rams.COMMENTS, 12,
                 'Approved. Add tower inspection record sheets to the site file.'),
                ('Kitchen wall cladding', 'Hygienic cladding to first floor kitchen', Rams.SUBMITTED, 1, ''),
            ]:
                Rams.objects.create(project=leeds, subcontractor=s_join, package=join_pkg, title=title, scope=scope,
                                    status=status, file=ContentFile(demo_pdf(title, f'RAMS · {s_join}'), name='rams.pdf'),
                                    original_name=f'{title}.pdf', submitted_by=subu,
                                    submitted_at=now - timedelta(days=days), review_comments=comments,
                                    reviewed_by=pm if status != Rams.SUBMITTED else None,
                                    reviewed_at=now - timedelta(days=days - 1) if status != Rams.SUBMITTED else None)
            sv1 = SubVariation.objects.create(
                project=leeds, subcontractor=s_join, package=join_pkg, title='Extra back-bar shelving', reason='INSTRUCTION',
                instructed_by='Priya, site walk 12 Oct', description='Two additional oak shelves and brackets to back bar.',
                value=Decimal('1200'), status=SubVariation.APPROVED, approved_value=Decimal('1100'),
                decision_notes='Agreed at £1,100 including fixings.', decided_by=pm, decided_at=now - timedelta(days=9),
                submitted_by=subu)
            SubVariation.objects.create(
                project=leeds, subcontractor=s_join, package=join_pkg, title='Bar front re-veneer after RFI-001',
                reason='DESIGN', instructed_by='RFI-001 response', value=Decimal('2350'), time_impact_days=2,
                description='Re-make bar front panels in smoked eucalyptus as confirmed in RFI-001.', submitted_by=subu)
            Notification.objects.create(user=pm, project=leeds, title=f'RAMS to review: Kitchen wall cladding ({s_join})',
                                        url=f'/{SLUG}/projects/{leeds.pk}/rams/')
            Notification.objects.create(user=subu, project=leeds, title=f'{sv1.reference} approved: {sv1.title}',
                                        url=f'/{SLUG}/projects/{leeds.pk}/sub-variations/{sv1.pk}/')

            Issue.objects.create(project=leeds, title='Damaged skirting to corridor C1', issue_type='SNAG',
                                 location='Ground floor corridor C1', assignee=subu, due_date=today + timedelta(days=5),
                                 created_by=pm)
            Issue.objects.create(project=leeds, title='Trailing cables at kitchen entrance', issue_type='SAFETY',
                                 location='First floor kitchen', assignee=pm, priority='URGENT', created_by=pm)

            for title, ref, cat, vis in [('General arrangement – ground floor', '1203-A-100', 'DRAWING', 'SHARED'),
                                         ('Bar joinery details', '1203-JD-04', 'DRAWING', 'SHARED'),
                                         ('Construction phase plan', 'CPP-01', 'RAMS', 'SHARED'),
                                         ('Cost plan – internal', 'CP-03', 'REPORT', 'INTERNAL')]:
                d = Document.objects.create(project=leeds, title=title, reference=ref, category=cat, visibility=vis,
                                            created_by=pm)
                for rev in (['P01', 'C01'] if cat == 'DRAWING' else ['P01']):
                    content = ContentFile(demo_pdf(title, f'{ref} revision {rev}'), name=f'{ref}-{rev}.pdf')
                    d.add_revision(DocumentRevision(revision=rev, file=content, original_name=f'{ref}-{rev}.pdf',
                                                    size=content.size, uploaded_by=pm))

            for verb, target, actor, internal in [('raised', f'{r1.reference} Bar back panelling', subu, False),
                                                  ('responded to', r3.reference, client, False),
                                                  ('submitted for approval', 'VO-001 Additional acoustic ceiling', pm, True),
                                                  ('moved to Submitted', 'Aparthotel refurb, Hammersmith', est, True)]:
                log_activity(actor, verb, target, project=None if 'Aparthotel' in target else leeds, internal_only=internal)

            # Kingsway: a live subcontract tender run through the portal
            kings = Project.objects.get(name='Office CAT B, Kingsway')
            due = kings.quotes_due
            # Something on the estimators' "Needs me" lists too
            Task.objects.create(project=kings, title='Chase drylining clarifications before the return', assignee=est,
                                due_date=today + timedelta(days=1), priority='HIGH', created_by=owner)
            Task.objects.create(project=kings, title='Agree tender margin and sign off the return', assignee=owner,
                                due_date=today + timedelta(days=4), created_by=est)
            for title, ref in [('Tender drawings – GA plans', 'KW-A-100'), ('Specification – fit-out', 'KW-SPEC-01'),
                               ('Reflected ceiling plans', 'KW-A-300')]:
                d = Document.objects.create(project=kings, title=title, reference=ref, category='TENDER',
                                            tender_issue=True, created_by=est)
                content = ContentFile(demo_pdf(title, f'{ref} · tender issue T1'), name=f'{ref}.pdf')
                d.add_revision(DocumentRevision(revision='T1', file=content, original_name=f'{ref}.pdf',
                                                size=content.size, uploaded_by=est))
            scopes = {
                'Drylining & partitions': 'Metal stud partitions, linings and bulkheads to levels 2 and 3 as drawings KW-A-100 '
                                          'and KW-A-300. Include fire-rated partitions to core and acoustic upgrade to meeting rooms.',
                'Joinery': 'Reception desk, tea point joinery and meeting room storage walls as specification section J.',
                'Electrical': 'Lighting, small power and data containment to CAT B layout. Lighting controls to spec section E4.',
            }
            bids = [
                (s_dry, 'Drylining & partitions', bidu, 'QUOTED', '148500'),
                (sub('Northern Partitions', ['Drylining & partitions'], contact_name='Sue Ward', email='sue@northern.example'),
                 'Drylining & partitions', None, 'QUOTED', '162300'),
                (sub('Ace Interiors', ['Drylining & partitions', 'Suspended ceilings'], email='tenders@ace.example'),
                 'Drylining & partitions', None, 'DECLINED', None),
                (s_join, 'Joinery', subu, 'ENQUIRY', None),
                (s_elec, 'Electrical', None, 'QUOTED', '96400'),
            ]
            for s_, trade_name, member, status, value in bids:
                Package.objects.create(
                    project=kings, subcontractor=s_, trade=t[trade_name], scope=scopes[trade_name], quote_due=due,
                    status=status, invited_by=est, sent_at=now - timedelta(days=12),
                    viewed_at=now - timedelta(days=11) if status != 'ENQUIRY' or member else None,
                    quote_value=Decimal(value) if value else None,
                    submitted_at=now - timedelta(days=2) if value else None,
                    valid_until=today + timedelta(days=60) if value else None,
                    exclusions='Out-of-hours working. Builders work in connection with services.' if value else '',
                    decline_reason='No capacity in November.' if status == 'DECLINED' else '')
                if member:
                    ProjectMember.objects.create(project=kings, user=member, role=ProjectMember.ROLE_BIDDER,
                                                 company_name=s_.company_name, subcontractor=s_, added_by=est)
            add1 = Addendum.objects.create(project=kings, title='Revised level 3 layout',
                                           description='Meeting rooms 3.04–3.06 reconfigured. Partition lengths increase by '
                                                       'about 18m. Revised GA attached.', issued_by=est)
            AddendumFile.objects.create(addendum=add1, file=ContentFile(demo_pdf('Revised level 3 layout', 'KW-A-103 revision T2'), name='KW-A-103-T2.pdf'),
                                        original_name='KW-A-103-T2.pdf', size=10)
            AddendumReceipt.objects.create(addendum=add1, subcontractor=s_dry, user=bidu)
            q1 = TenderQuery.objects.create(project=kings, subcontractor=s_dry, asked_by=bidu,
                                            question='Are the bulkheads to the reception included in drylining or ceilings?',
                                            answer='Drylining. Ceilings package covers MF grid only.', answered_by=est,
                                            answered_at=now - timedelta(days=4), published=True)
            TenderQuery.objects.create(project=kings, subcontractor=s_join, asked_by=subu,
                                       question='Can we offer an alternative laminate to the specified veneer?')
            for u_, title in [(est, f'Quote received: {s_dry} for Drylining & partitions'),
                              (est, f'Tender query {q1.reference} from {s_dry}'),
                              (est, 'Declined to price: Ace Interiors for Drylining & partitions'),
                              (subu, f'Invitation to tender: Joinery on {kings.name}'),
                              (bidu, f'{add1.reference} issued: {add1.title}')]:
                Notification.objects.create(user=u_, project=kings, title=title,
                                            url=f'/{SLUG}/projects/{kings.pk}/tender/')

        self.stdout.write(self.style.SUCCESS(f'Demo workspace ready at /{SLUG}/  (password {PASSWORD})'))
