"""Bid discipline pages, included under /<org_slug>/ by tenancy/urls_org.py."""
from django.urls import path

from . import bid_views

P = 'projects/<int:pk>/'

urlpatterns = [
    path(P + 'bid/', bid_views.bid_gate, name='bid_gate'),
    path(P + 'bid/decline/', bid_views.bid_decline, name='bid_decline'),
    path(P + 'bid/lost/', bid_views.bid_lost, name='bid_lost'),
    path('tenders/coverage/', bid_views.coverage_board, name='tender_coverage'),
    path('estimating/settings/', bid_views.tender_settings, name='tender_settings'),
]
