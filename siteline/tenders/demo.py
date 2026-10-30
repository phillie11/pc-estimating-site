"""
Demo data for bid discipline, called at the end of tenancy's seed_demo inside org_context:
bid/no-bid scores on open and decided bids, decline reasons, loss reviews, a lead that must
pass the gate and one that can skip it, structured regions, and a thin tender due today.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from directory.models import Package, Subcontractor, Trade
from directory.regions import guess_regions
from projects.models import Project, ProjectInvite, ProjectMember

from .models import BidReview, TenderSettings


def seed(owner, estimator):
    now, today = timezone.now(), timezone.localdate()
    for s in Subcontractor.objects.all():
        s.region_codes = guess_regions(s.regions)
        s.save(update_fields=['region_codes'])
    TenderSettings.objects.create()  # defaults: check required from £250,000, 3 quotes per trade

    def review(name, points=None, **kw):
        p = Project.objects.get(name=name)
        r = BidReview(project=p, **kw)
        if points:
            for key, value in zip(('client', 'sector', 'value', 'programme', 'workload', 'terms'), points):
                setattr(r, f'q_{key}', value)
            r.rescore()
            r.decision = r.decision or BidReview.PROCEED
            r.assessed_by, r.assessed_at = estimator, now - timedelta(days=20)
        r.save()
        return r

    # Open bids
    review('Office CAT B, Kingsway', (4, 4, 4, 2, 2, 2), contract_form='JCT_DB', bidders=4, workload_bids=2,
           amendments='Client schedule of amendments: LADs £7,500/week, 5% retention, no cap on design liability.')
    review('Jewellery store, Argyle Arcade', (3, 4, 2, 2, 1, 2), contract_form='JCT_IC', bidders=5, workload_bids=4,
           decision_note='Framework call-off. Tight programme but a client we want.')
    # Decided bids, so tender analytics shows win rate by score, declines and losses
    review('Café chain rollout, phase 2', (4, 4, 4, 4, 2, 4))
    review('Flagship store, King Street', (4, 4, 2, 4, 4, 4))
    review('Barbers, Northern Quarter', (3, 2, 2, 2, 2, 4))
    review('Dental practice, Chester', (2, 2, 2, 2, 2, 2), loss_reason='RELATIONSHIP', our_price=Decimal('96000'),
           lessons='Client used a local contractor they already knew. Meet the decision maker before pricing.')
    review('Office refurb, Spinningfields', (2, 2, 0, 0, 2, 2), loss_reason='PROGRAMME',
           decision_note='Bid against a No-go: key Arden Property relationship.', our_price=Decimal('2300000'),
           lessons='Programme 6 weeks longer than the winner. Get the programme reviewed by operations before submitting.')
    review('Hotel bedrooms, Harrogate', (3, 2, 2, 2, 3, 2), loss_reason='NO_FEEDBACK', our_price=Decimal('1100000'))
    review('Car showroom, Preston', (4, 4, 2, 4, 2, 2), loss_reason='PRICE', our_price=Decimal('880000'),
           winner_price=Decimal('860000'), lessons='Lost on price, 2.3% higher. Drylining and M&E quotes were high.')
    review('College refurbishment, Pershore', (2, 2, 2, 4, 2, 2), loss_reason='PRICE', our_price=Decimal('960000'),
           winner_price=Decimal('922000'), lessons='2nd of 5. Preliminaries 1.5% above the others.')
    review('Primary school toilets, Wirral', decision=BidReview.DECLINED, decline_reason='BIDDERS',
           decline_detail='Seven contractors on the list.', bidders=7)

    # A small lead the estimator can push straight to Tendering (the check is optional under £250k);
    # the Gym lead (£420k) has to pass it.
    Project.objects.create(name='Barbers refit, Didsbury', stage=Project.STAGE_LEAD, value=Decimal('85000'),
                           probability=40, client_name='Fade Co', site_address='Didsbury, Manchester', sector='RETAIL',
                           owner=estimator, created_by=owner, lead_source='Repeat client',
                           tender_received=today, tender_due=(now + timedelta(days=12)).replace(hour=12, minute=0))

    # Argyle: sub quotes are due today and Electrical has nobody quoting yet, so it shows on the
    # company-wide coverage board and in coverage_alerts (Needs me / digest)
    argyle = Project.objects.get(name='Jewellery store, Argyle Arcade')
    elec = Subcontractor.objects.get(company_name='Brightside Electrical')
    Package.objects.create(project=argyle, subcontractor=elec, trade=Trade.objects.get(name='Electrical'),
                           scope='Lighting and small power to the shop floor and back of house.',
                           quote_due=argyle.quotes_due, status=Package.ENQUIRY, invited_by=estimator,
                           sent_at=now - timedelta(days=9))
    ProjectInvite.objects.create(project=argyle, email=elec.email, role=ProjectMember.ROLE_BIDDER,
                                 company_name=elec.company_name, subcontractor=elec, invited_by=estimator)
    # Kingsway's joinery invitation was chased yesterday
    kings = Project.objects.get(name='Office CAT B, Kingsway')
    Package.objects.filter(project=kings, status=Package.ENQUIRY).update(chased_at=now - timedelta(days=1), chase_count=1)
