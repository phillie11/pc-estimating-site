"""
The Procurement tab's URLs. tenancy/urls_org.py splices these in with its project prefix, so they
stay plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views


def project_patterns(P):
    return [
        path(P + 'procurement/', views.schedule, name='procurement_schedule'),
        path(P + 'procurement/new/', views.line_create, name='procurement_line_create'),
        path(P + 'procurement/<int:line_id>/', views.line_detail, name='procurement_line'),
        path(P + 'procurement/settings/', views.procurement_settings, name='procurement_settings'),
        path(P + 'procurement/submittals/', views.submittal_list, name='submittal_list'),
        path(P + 'procurement/submittals/new/', views.submittal_create, name='submittal_create'),
        path(P + 'procurement/submittals/<int:sub_id>/', views.submittal_detail, name='submittal_detail'),
        path(P + 'procurement/submittals/<int:sub_id>/edit/', views.submittal_edit, name='submittal_edit'),
        path(P + 'procurement/submittals/<int:sub_id>/files/<int:file_id>/', views.submittal_file,
             name='submittal_file'),
    ]
