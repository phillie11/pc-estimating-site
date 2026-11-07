"""Company reports, owners and admins (tenancy/urls_org.py splices these in)."""
from django.urls import path

from . import views

org_patterns = [
    path('reports/', views.reports_home, name='reports_home'),
    path('reports/margin/', views.report_margin, name='report_margin'),
    path('reports/cash/', views.report_cash, name='report_cash'),
    path('reports/overdue/', views.report_overdue, name='report_overdue'),
    path('reports/safety/', views.report_safety, name='report_safety'),
    path('reports/subcontractors/', views.report_subcontractors, name='report_subcontractors'),
]
