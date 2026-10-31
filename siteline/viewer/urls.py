"""
The drawing viewer's URLs, under the Documents tab. tenancy/urls_org.py splices these in with
its project prefix, so they stay plain entries in its urlpatterns (the archived-project test
walks that list).
"""
from django.urls import path

from . import views


def project_patterns(P):
    D = P + 'documents/'
    return [
        path(D + '<int:doc_id>/rev/<int:rev_id>/viewer/', views.drawing_viewer, name='drawing_viewer'),
        path(D + '<int:doc_id>/rev/<int:rev_id>/pdf/', views.drawing_pdf, name='drawing_pdf'),
        path(D + '<int:doc_id>/pins/', views.pin_create, name='pin_create'),
        path(D + '<int:doc_id>/pins/<int:pin_id>/delete/', views.pin_delete, name='pin_delete'),
        path(D + 'pins/<int:pin_id>/', views.pin_open, name='pin_open'),
    ]
