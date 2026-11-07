"""
Commercial > CVR, Cashflow and Dayworks, the company CVR and Settings > Commercial reporting.
tenancy/urls_org.py splices these in with its project prefix, so they stay plain entries in its
urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import portal, views


def project_patterns(P):
    C = P + 'commercial/'
    return [
        path(C + 'cvr/', views.cvr_list, name='cvr_list'),
        path(C + 'cvr/<int:period_id>/', views.cvr_period, name='cvr_period'),
        path(C + 'cvr/<int:period_id>/adjustments/<int:adj_id>/reverse/', views.cvr_reverse,
             name='cvr_reverse'),
        path(C + 'cashflow/', views.cashflow, name='cvr_cashflow'),
        path(C + 'dayworks/', views.daywork_list, name='daywork_list'),
        path(C + 'dayworks/new/', views.daywork_create, name='daywork_create'),
        path(C + 'dayworks/rates/', views.daywork_rates, name='daywork_rates'),
        path(C + 'dayworks/<int:sheet_id>/', views.daywork_detail, name='daywork_detail'),
        path(C + 'dayworks/<int:sheet_id>/edit/', views.daywork_edit, name='daywork_edit'),
        path(C + 'dayworks/<int:sheet_id>/sign/', views.daywork_sign, name='daywork_sign'),
        path(C + 'dayworks/<int:sheet_id>/signature/', views.daywork_signature, name='daywork_signature'),
        path(C + 'dayworks/<int:sheet_id>/photos/<int:photo_id>/<str:size>/', views.daywork_photo,
             name='daywork_photo'),
        # The client or consultant asked to sign a sheet in their portal (cvr/portal.py)
        path(P + 'dayworks-to-sign/', portal.daywork_list, name='daywork_portal'),
        path(P + 'dayworks-to-sign/<int:sheet_id>/', portal.daywork_sign, name='daywork_portal_sign'),
        path(P + 'dayworks-to-sign/<int:sheet_id>/photos/<int:photo_id>/<str:size>/', portal.daywork_photo,
             name='daywork_portal_photo'),
    ]


org_patterns = [
    path('cvr/', views.company_cvr, name='company_cvr'),
    path('settings/commercial-reporting/', views.cvr_settings, name='cvr_settings'),
]
