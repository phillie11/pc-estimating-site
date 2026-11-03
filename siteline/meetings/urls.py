"""
The Meetings tab's URLs. tenancy/urls_org.py splices these in with its project prefix, so they stay
plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views


def project_patterns(P):
    M = P + 'meetings/'
    return [
        path(M, views.meeting_list, name='meeting_list'),
        path(M + 'new/', views.meeting_create, name='meeting_create'),
        path(M + 'actions/', views.action_register, name='meeting_actions'),
        path(M + 'actions/<int:action_id>/', views.action_update, name='meeting_action_update'),
        path(M + '<int:meeting_id>/', views.meeting_detail, name='meeting_detail'),
        path(M + '<int:meeting_id>/edit/', views.meeting_edit, name='meeting_edit'),
    ]


org_patterns = [
    path('meetings/agendas/', views.agenda_settings, name='meeting_agendas'),
]
