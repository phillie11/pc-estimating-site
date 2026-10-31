"""
Applications for payment. tenancy/urls_org.py splices these in with its project prefix, so they
stay plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views


def project_patterns(P):
    return [
        path(P + 'applications/', views.application_list, name='application_list'),
        path(P + 'applications/new/', views.application_create, name='application_create'),
        path(P + 'applications/<int:app_id>/', views.application_detail, name='application_detail'),
        path(P + 'applications/<int:app_id>/edit/', views.application_edit, name='application_edit'),
        path(P + 'applications/<int:app_id>/pdf/', views.application_pdf_view, name='application_pdf'),
        path(P + 'applications/<int:app_id>/evidence/<slug:kind>/', views.application_evidence,
             name='application_evidence'),
    ]
