"""
Subcontractor payments. tenancy/urls_org.py splices these in with its project prefix, so they
stay plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views


def project_patterns(P):
    S = P + 'sub-payments/'
    return [
        path(S, views.subpay_list, name='subpay_list'),
        path(S + 'package/<int:package_id>/', views.subpay_open, name='subpay_open'),
        path(S + 'package/<int:package_id>/apply/', views.subpay_apply, name='subpay_apply'),
        path(S + 'subcontract/<int:sc_id>/', views.subpay_subcontract, name='subpay_subcontract'),
        path(S + 'subcontract/<int:sc_id>/terms/', views.subpay_terms, name='subpay_terms'),
        path(S + 'subcontract/<int:sc_id>/release/', views.subpay_release, name='subpay_release'),
        path(S + 'applications/<int:app_id>/', views.subpay_application, name='subpay_application'),
        path(S + 'payments/<int:payment_id>/advice/', views.subpay_advice, name='subpay_advice'),
    ]


org_patterns = [
    path('settings/sub-payments/', views.subpay_settings, name='subpay_settings'),
    path('sub-payments/cis/', views.subpay_cis, name='subpay_cis'),
    path('sub-payments/subcontractors/', views.subpay_tax_list, name='subpay_tax_list'),
    path('sub-payments/subcontractors/<int:sub_id>/', views.subpay_tax, name='subpay_tax'),
]
