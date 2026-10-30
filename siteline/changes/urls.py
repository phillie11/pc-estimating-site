"""
The Commercial tab's URLs (contract set-up, change register, early warnings, change events) and the
company-wide change register. tenancy/urls_org.py splices these in with its project prefix, so
they stay plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from contracts import views as contracts

from . import views


def project_patterns(P):
    return [
        path(P + 'commercial/', contracts.contract_detail, name='contract_detail'),
        path(P + 'commercial/contract/', contracts.contract_edit, name='contract_edit'),
        path(P + 'commercial/changes/', views.change_register, name='change_register'),
        path(P + 'commercial/early-warnings/new/', views.early_warning_create, name='early_warning_create'),
        path(P + 'commercial/early-warnings/<int:ew_id>/', views.early_warning_detail, name='early_warning_detail'),
        path(P + 'commercial/early-warnings/<int:ew_id>/edit/', views.early_warning_edit, name='early_warning_edit'),
        path(P + 'commercial/change-events/new/', views.change_event_create, name='change_event_create'),
        path(P + 'commercial/change-events/<int:ce_id>/', views.change_event_detail, name='change_event_detail'),
        path(P + 'commercial/change-events/<int:ce_id>/edit/', views.change_event_edit, name='change_event_edit'),
    ]


org_patterns = [
    path('changes/', views.company_change_register, name='company_change_register'),
]
