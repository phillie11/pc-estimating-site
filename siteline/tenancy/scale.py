"""
A large company for performance work: `manage.py seed_scale` and the query-count tests.

At the default size it is roughly a contractor two years in: 200 jobs (half live), 40 staff,
300 subcontractors, about 20,000 cost ledger entries, 3,000 RFIs, 5,000 diary entries, 10,000
documents and revisions and 2,000 applications for payment (client and subcontractor), with the
packages, orders, variations, snags, tasks, submittals, activity and notifications around them.

Numbered registers (RFIs, diary entries, variations, applications) are saved the normal way
(`save(actor=...)`) so their numbering, record index and audit trail are real; plain tables
(documents, ledger, activity) are bulk-inserted. Everything is inside `org_context(org)`.

`build(name, jobs=..., ...)` takes smaller numbers for tests: a query-count test builds a small and
a larger company and checks a page's query count doesn't grow with it.
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

PASSWORD = 'ScalePass!2026'


def _money(rng, lo, hi):
    return Decimal(rng.randrange(lo * 100, hi * 100)) / 100


class Built:
    """Handles on what was made, for crawls and tests."""

    def __init__(self, org):
        self.org = org
        self.owner = self.pm = self.estimator = self.client = self.sub_user = None
        self.live = []
        self.bids = []
        self.users = {}


@transaction.atomic
def build(name='Scale Fitout', jobs=200, staff=40, subs=300, rfis_per_job=30, diary_per_job=50,
          docs_per_job=50, apps_per_job=12, subapps_per_job=8, actuals_per_job=170, seed=7, email_domain='scale.test',
          stdout=None):
    from accounts.models import User
    from accounts.views import create_organization
    from changes.models import ChangeEvent
    from contracts.models import MainContract
    from core.workflow import apply_transition
    from costs import services as cost_services
    from costs.access import settings_for
    from costs.models import CostCode, LedgerEntry
    from diary.models import DelayLine, DiaryEntry
    from directory.models import Package, Subcontractor, Trade
    from payments.models import Application, Payment
    from procurement import services as proc_services
    from procurement.models import ScheduleLine, Submittal
    from projects.models import Document, DocumentRevision, Issue, Project, ProjectMember, Task
    from rfis.models import RFI
    from subpayments import services as sub_services
    from subpayments.models import SubcontractorTax, subcontract_for
    from tenancy.context import org_context
    from tenancy.models import ActivityEvent, Membership, Notification
    from variations.models import Variation

    rng = random.Random(seed)
    say = (lambda m: stdout.write(m)) if stdout else (lambda m: None)
    today = timezone.localdate()
    now = timezone.now()

    def mkuser(email, full):
        u = User(email=email, full_name=full)
        u.set_password(PASSWORD)
        return u

    # --- people ---------------------------------------------------------------------------
    owner = User.objects.create_user(email=f'owner@{email_domain}', password=PASSWORD, full_name='Olivia Owner')
    org = create_organization(name, owner)
    org.plan = 'BUSINESS'
    org.subscription_status = 'active'
    org.save()
    out = Built(org)
    out.owner = owner
    # Hashing 40 passwords is the slow part of user creation: hash once, copy the hash
    proto = mkuser('x@x', 'x')
    hashed = proto.password
    people = []
    for i in range(staff - 1):
        people.append(User(email=f'staff{i:02d}@{email_domain}', full_name=f'Staff Member {i:02d}', password=hashed))
    User.objects.bulk_create(people)
    people = list(User.objects.filter(email__startswith='staff', email__endswith=email_domain).order_by('email'))
    mems = []
    estimators, pms = [], []
    for i, u in enumerate(people):
        if i < 2:
            role, area = Membership.ROLE_ADMIN, Membership.AREA_BOTH
        else:
            role = Membership.ROLE_MEMBER
            area = (Membership.AREA_ESTIMATING, Membership.AREA_DELIVERY, Membership.AREA_BOTH)[(i - 2) % 3]
        if area in (Membership.AREA_ESTIMATING, Membership.AREA_BOTH):
            estimators.append(u)
        if area in (Membership.AREA_DELIVERY, Membership.AREA_BOTH):
            pms.append(u)
        mems.append(Membership(organization=org, user=u, role=role, area=area))
    Membership.objects.bulk_create(mems)
    out.estimator = next(u for u, m in zip(people, mems) if m.area == Membership.AREA_ESTIMATING)
    out.pm = next(u for u, m in zip(people, mems) if m.area == Membership.AREA_DELIVERY)

    with org_context(org):
        trades = list(Trade.objects.all())
        # --- subcontractors, half with portal users -----------------------------------------
        sub_rows = []
        for i in range(subs):
            sub_rows.append(Subcontractor(
                organization=org, company_name=f'Sub {i:03d} {trades[i % len(trades)].name} Ltd',
                email=f'sub{i:03d}@{email_domain}', contact_name=f'Contact {i:03d}',
                status=Subcontractor.STATUS_APPROVED, rating=1 + i % 5,
                public_liability_expiry=today + timedelta(days=rng.randrange(-30, 400)),
                employers_liability_expiry=today + timedelta(days=rng.randrange(-30, 400))))
        Subcontractor.objects.bulk_create(sub_rows)
        sub_list = list(Subcontractor.objects.order_by('pk'))
        Through = Subcontractor.trades.through
        Through.objects.bulk_create([Through(subcontractor_id=s.pk, trade_id=trades[i % len(trades)].pk)
                                     for i, s in enumerate(sub_list)])
        sub_users = [User(email=s.email, full_name=s.contact_name, password=hashed) for s in sub_list[: subs // 2]]
        User.objects.bulk_create(sub_users)
        sub_users = {u.email: u for u in User.objects.filter(email__startswith='sub', email__endswith=email_domain)}
        Membership.objects.bulk_create([Membership(organization=org, user=u, role=Membership.ROLE_EXTERNAL)
                                        for u in sub_users.values()])
        SubcontractorTax.objects.bulk_create([SubcontractorTax(
            organization=org, subcontractor=s, cis_status=('GROSS', 'REGISTERED', 'UNVERIFIED')[i % 3],
            reverse_charge=bool(i % 2)) for i, s in enumerate(sub_list)])
        say(f'{len(people) + 1} staff, {len(sub_list)} subcontractors')

        settings_for(org)
        codes = list(CostCode.objects.filter(is_active=True).order_by('code'))
        trade_code = {c.name.lower(): c for c in codes}

        # --- jobs ----------------------------------------------------------------------------
        n_live = jobs // 2
        n_bid = int(jobs * 0.3)
        stages = ([Project.STAGE_LIVE] * (n_live - n_live // 10) + [Project.STAGE_WON] * (n_live // 10)
                  + [Project.STAGE_LEAD, Project.STAGE_TENDERING, Project.STAGE_SUBMITTED] * (n_bid // 3 + 1))
        stages = stages[:n_live + n_bid]
        stages += [Project.STAGE_COMPLETE, Project.STAGE_LOST, Project.STAGE_DECLINED] * jobs
        stages = stages[:jobs]
        project_rows = []
        for i, stage in enumerate(stages):
            live = stage in Project.DELIVERY_STAGES
            start = today - timedelta(days=rng.randrange(60, 600)) if live else None
            project_rows.append(Project(
                organization=org, name=f'Job {i:03d} {rng.choice(["Bar", "Office", "Retail", "Clinic", "Hotel"])} fit-out',
                stage=stage, value=_money(rng, 150_000, 3_000_000), client_name=f'Client {i % 60:02d} Ltd',
                site_address=f'{i} High Street, Leeds LS{i % 30}', owner=estimators[i % len(estimators)],
                manager=(out.pm if i == 0 else pms[i % len(pms)]) if live or stage == Project.STAGE_COMPLETE else None,
                probability=rng.randrange(10, 90), start_date=start,
                completion_date=(start + timedelta(days=rng.randrange(120, 700))) if start else None,
                tender_received=today - timedelta(days=rng.randrange(5, 60)) if not live else None,
                tender_due=now + timedelta(days=rng.randrange(-20, 40)) if not live else None,
                quotes_due=today + timedelta(days=rng.randrange(-10, 30)) if not live else None,
                created_by=owner, handed_over_at=now - timedelta(days=30) if live else None))
        # Project numbering (reference) is done in save(); create one by one
        projects = []
        for p in project_rows:
            p.save()
            projects.append(p)
        live = [p for p in projects if p.stage in Project.DELIVERY_STAGES]
        out.live, out.bids = live, [p for p in projects if p.stage in Project.PIPELINE_STAGES]
        say(f'{len(projects)} jobs, {len(live)} live')

        # --- external people on live jobs ---------------------------------------------------
        clients = [User(email=f'client{i:03d}@{email_domain}', full_name=f'Client Person {i:03d}', password=hashed)
                   for i in range(len(live))]
        User.objects.bulk_create(clients)
        clients = list(User.objects.filter(email__startswith='client', email__endswith=email_domain).order_by('email'))
        Membership.objects.bulk_create([Membership(organization=org, user=u, role=Membership.ROLE_EXTERNAL)
                                        for u in clients])
        out.client = clients[0]
        members = []
        for p, c in zip(live, clients):
            members.append(ProjectMember(organization=org, project=p, user=c, role=ProjectMember.ROLE_CLIENT,
                                         company_name=p.client_name))
            members.append(ProjectMember(organization=org, project=p, user=p.manager, role=ProjectMember.ROLE_PM))
        # A second client on the first job, so the client crawl has more than one project
        for p in live[1:4]:
            members.append(ProjectMember(organization=org, project=p, user=clients[0], role=ProjectMember.ROLE_CLIENT))

        # --- packages: 8 appointed per live job, 6 trades x 3 bidders per open bid -----------
        pkg_rows = []

        for j, p in enumerate(live):
            for k in range(8):
                s = sub_list[(j * 8 + k) % len(sub_list)]
                pkg_rows.append(Package(organization=org, project=p, subcontractor=s, trade=trades[(j + k) % len(trades)],
                                        status=Package.APPOINTED, quote_value=_money(rng, 10_000, 250_000),
                                        submitted_at=now - timedelta(days=90), decided_at=now - timedelta(days=80),
                                        scope='As drawings and specification.'))
        for j, p in enumerate(out.bids):
            for k in range(6):
                for b in range(3):
                    s = sub_list[(j * 18 + k * 3 + b + 7) % len(sub_list)]
                    quoted = rng.random() < 0.6
                    pkg_rows.append(Package(organization=org, project=p, subcontractor=s,
                                            trade=trades[(j + k) % len(trades)],
                                            status=Package.QUOTED if quoted else Package.ENQUIRY,
                                            quote_value=_money(rng, 10_000, 250_000) if quoted else None,
                                            submitted_at=now - timedelta(days=3) if quoted else None,
                                            quote_due=today + timedelta(days=rng.randrange(-5, 20)),
                                            scope='As drawings and specification.'))
        Package.objects.bulk_create(pkg_rows)
        packages = list(Package.objects.select_related('subcontractor', 'trade', 'project').order_by('pk'))
        seen = set()
        for pkg in packages:
            u = sub_users.get(pkg.subcontractor.email)
            if u is None or (pkg.project_id, u.pk) in seen:
                continue
            seen.add((pkg.project_id, u.pk))
            role = ProjectMember.ROLE_SUBCONTRACTOR if pkg.status == Package.APPOINTED else ProjectMember.ROLE_BIDDER
            members.append(ProjectMember(organization=org, project_id=pkg.project_id, user=u, role=role,
                                         subcontractor=pkg.subcontractor, company_name=pkg.subcontractor.company_name))
        ProjectMember.objects.bulk_create(members)
        appointed = [p for p in packages if p.status == Package.APPOINTED]
        out.sub_user = sub_users[appointed[0].subcontractor.email]
        say(f'{len(packages)} packages')

        # --- contracts, budgets, orders, ledger ---------------------------------------------
        ct = ContentType.objects.get_for_model(Package)
        ledger_rows = []
        for j, p in enumerate(live):
            MainContract.objects.create(organization=org, project=p, form='JCT_IC', retention_percent=Decimal('5'),
                                        valuation_day=25, due_date_offset_days=7, final_date_offset_days=14,
                                        pay_less_notice_days=5, payment_notice_days=5)
            target = (p.value * Decimal('0.9')).quantize(Decimal('0.01'))
            spread = codes[:10]
            each = (target / len(spread)).quantize(Decimal('0.01'))
            lines = [(c, '', each) for c in spread[:-1]] + [(spread[-1], '', target - each * (len(spread) - 1))]
            cost_services.set_original_budget(p, owner, p.value, Decimal('10'), lines)
        for pkg in appointed:
            code = trade_code.get(pkg.trade.name.lower()) or codes[-1]
            cost_services.commit_package(pkg, code, owner)
        for j, p in enumerate(live):
            for k in range(actuals_per_job):
                ledger_rows.append(LedgerEntry(
                    organization=org, project=p, cost_code=codes[k % len(codes)], kind='ACTUAL',
                    amount=_money(rng, 50, 4000), date=today - timedelta(days=rng.randrange(1, 500)),
                    source_type=ct, source_id=appointed[(j * 8 + k) % len(appointed)].pk,
                    actor=owner, actor_name=owner.full_name, note=f'Invoice {k}'))
        LedgerEntry.objects.bulk_create(ledger_rows, batch_size=2000)
        say(f'{LedgerEntry.objects.count()} ledger entries')

        # --- documents and revisions ---------------------------------------------------------
        doc_rows = []
        for j, p in enumerate(live):
            for k in range(docs_per_job):
                doc_rows.append(Document(organization=org, project=p, title=f'Drawing {k:03d}',
                                         reference=f'J{j:03d}-ABC-ZZ-0{k % 4}-DR-A-{1000 + k}', category='DRAWING',
                                         discipline='Architectural', visibility=Document.VIS_SHARED if k % 2 else
                                         Document.VIS_INTERNAL, created_by=p.manager))
        Document.objects.bulk_create(doc_rows, batch_size=2000)
        docs = list(Document.objects.order_by('pk'))
        rev_rows = []
        for i, d in enumerate(docs):
            n = 1 + i % 2  # 1 or 2 revisions: about 1.5 per document
            for r in range(n):
                current = r == n - 1
                rev_rows.append(DocumentRevision(
                    organization=org, document=d, file=f'scale/{d.pk}-P0{r + 1}.pdf', original_name=f'{d.reference}-P0{r + 1}.pdf',
                    size=120_000, revision=f'P0{r + 1}', suitability='S2', status='SHARED' if current else 'SUPERSEDED',
                    is_current=current, uploaded_by=d.created_by, shared_at=now - timedelta(days=10), page_count=2))
        DocumentRevision.objects.bulk_create(rev_rows, batch_size=2000)
        say(f'{len(docs)} documents, {len(rev_rows)} revisions')

        # --- numbered registers, saved properly ----------------------------------------------
        for j, p in enumerate(live):
            client = clients[j]
            for k in range(rfis_per_job):
                ball = client if k % 3 == 0 else p.manager
                rfi = RFI(project=p, title=f'Question {k}', question='Which detail applies?', ball_in_court_user=ball,
                          due_date=today + timedelta(days=rng.randrange(-20, 20)),
                          visibility='SHARED' if k % 2 else 'INTERNAL')
                if k % 4 == 0:
                    rfi.status = 'CLOSED'
                rfi.save(actor=p.manager)
        say(f'{RFI.objects.count()} RFIs')
        for j, p in enumerate(live):
            for k in range(diary_per_job):
                day = today - timedelta(days=k + 1)
                e = DiaryEntry(project=p, entry_date=day, title=f'Diary {day}', notes='Progress as planned.',
                               weather_am='DRY', weather_pm='DRY')
                e.save(actor=p.manager)
                if k % 10 == 0:
                    DelayLine.objects.create(organization=org, entry=e, cause='WEATHER', hours_lost=Decimal('2'),
                                         description='Rain stopped external works', created_by=p.manager)
        say(f'{DiaryEntry.objects.count()} diary entries')
        for j, p in enumerate(live):
            for k in range(10):
                vo = Variation(project=p, title=f'Change {k}', description='Client instruction.',
                               value=_money(rng, 500, 20000))
                vo.save(actor=p.manager)
                if k % 3 == 0:
                    apply_transition(vo, 'submit', p.manager)
            for k in range(4):
                ChangeEvent(project=p, title=f'Event {k}', description='x', ball_in_court_user=p.manager,
                            aware_on=today).save(actor=p.manager)
        say(f'{Variation.objects.count()} variations')

        # --- applications for payment: monthly, oldest first ---------------------------------
        for j, p in enumerate(live):
            contract = MainContract.objects.get(project=p)
            work = Decimal('0')
            for k in range(apps_per_job):
                months_ago = apps_per_job - k
                valuation = (today.replace(day=1) - timedelta(days=30 * months_ago)).replace(day=25)
                if valuation >= today:
                    continue
                work += (p.value / (apps_per_job + 4)).quantize(Decimal('0.01'))
                app = Application(project=p, valuation_date=valuation, work_done=work,
                                  title=f'Valuation to {valuation:%d %b %Y}')
                app.recalculate(contract)
                app.save(actor=p.manager)
                apply_transition(app, 'submit', p.manager)
                if k < apps_per_job - 2:
                    Payment.objects.create(organization=org, application=app, amount=app.total,
                                           received_on=valuation + timedelta(days=20), recorded_by=p.manager)
                    app._paid_total = None
                    apply_transition(app, 'paid', p.manager, paid_on=valuation + timedelta(days=20))
        say(f'{Application.objects.count()} applications')
        sub_apps = 0
        for j, p in enumerate(live):
            for pkg in [x for x in appointed if x.project_id == p.pk][:max(1, subapps_per_job // 4)]:
                sc = subcontract_for(pkg, owner)
                gross = Decimal('0')
                for k in range(4):
                    valuation = (today.replace(day=1) - timedelta(days=30 * (4 - k))).replace(day=25)
                    gross += (pkg.quote_value / 6).quantize(Decimal('0.01'))
                    try:
                        sa = sub_services.receive_application(sc, owner, valuation, gross, check_date=False)
                    except sub_services.SubPayError:
                        continue
                    sub_apps += 1
                    if k < 3:
                        try:
                            apply_transition(sa, 'payment_notice', owner, amount=sa.amount_due, basis='As applied')
                            sub_services.record_payment(sa, owner, sa.amount_due, valuation + timedelta(days=25))
                        except Exception:  # noqa: BLE001 - a dataset, not a test: skip a cycle that doesn't fit
                            pass
        say(f'{sub_apps} sub applications')

        # --- procurement, submittals, snags, tasks ------------------------------------------
        for j, p in enumerate(live):
            mine = [x for x in appointed if x.project_id == p.pk]
            for k, pkg in enumerate(mine[:6]):
                line = ScheduleLine(project=p, title=str(pkg.trade), package=pkg, required_on_site=today + timedelta(
                    days=rng.randrange(-30, 120)), lead_time_weeks=rng.randrange(2, 12), approval_weeks=2,
                    status='APPOINTED', created_by=p.manager)
                line.save()
                if k < 2:
                    s = Submittal(project=p, line=line, package=pkg, title=f'{line.title} shop drawings',
                                  kind='SHOP_DRAWING', due_date=line.submittals_due)
                    proc_services.initial_ball(s)
                    s.save(actor=p.manager)
        issue_rows, task_rows = [], []
        for j, p in enumerate(live):
            mine = [x for x in appointed if x.project_id == p.pk]
            for k in range(30):
                assignee = sub_users.get(mine[k % len(mine)].subcontractor.email) if mine and k % 2 else p.manager
                issue_rows.append(Issue(organization=org, project=p, number=k + 1, title=f'Snag {k}', assignee=assignee,
                                        status='OPEN' if k % 3 else 'CLOSED', due_date=today + timedelta(days=k - 15),
                                        created_by=p.manager))
            for k in range(10):
                task_rows.append(Task(organization=org, project=p, title=f'Task {k}', assignee=p.manager,
                                      due_date=today + timedelta(days=k - 3), created_by=p.manager))
        Issue.objects.bulk_create(issue_rows, batch_size=2000)
        Task.objects.bulk_create(task_rows, batch_size=2000)

        # --- activity and notifications -------------------------------------------------------
        act = []
        for i in range(20_000 if jobs >= 100 else jobs * 40):
            p = projects[i % len(projects)]
            act.append(ActivityEvent(organization=org, actor=owner, project=p, verb='updated', target=f'Item {i}',
                                     internal_only=bool(i % 3)))
        ActivityEvent.objects.bulk_create(act, batch_size=2000)
        notes = []
        everyone = [owner, out.pm, out.estimator, out.client, out.sub_user]
        for i in range(10_000 if jobs >= 100 else jobs * 20):
            notes.append(Notification(organization=org, user=everyone[i % len(everyone)], actor=owner,
                                      project=projects[i % len(projects)], title=f'Something happened {i}', url='/'))
        Notification.objects.bulk_create(notes, batch_size=2000)
        say('activity and notifications done')
    out.users = {'owner': owner, 'pm': out.pm, 'estimator': out.estimator, 'client': out.client, 'sub': out.sub_user}
    out.slug = org.slug
    return out
