"""
The Safety tab's URLs. tenancy/urls_org.py splices project_patterns and org_patterns in with its
project prefix, so they stay plain entries in its urlpatterns (the archived-project test walks
that list). public_patterns are the QR sign-in page, outside any workspace (config/urls.py).
"""
from django.urls import path

from . import portal, public, views


def project_patterns(P):
    S = P + 'safety/'
    I = S + 'incidents/<int:incident_id>/'  # noqa: E741
    return [
        path(S, views.safety_home, name='safety_home'),
        path(S + 'incidents/', views.incident_list, name='incident_list'),
        path(S + 'incidents/report/', views.incident_report, name='incident_report'),
        path(I, views.incident_detail, name='incident_detail'),
        path(I + 'edit/', views.incident_edit, name='incident_edit'),
        path(I + 'status/', views.incident_transition, name='incident_transition'),
        path(I + 'actions/add/', views.incident_action_add, name='incident_action_add'),
        path(I + 'actions/<int:action_id>/', views.incident_action_update, name='incident_action_update'),
        path(I + 'photos/add/', views.incident_photo_add, name='incident_photo_add'),
        path(I + 'photos/<int:photo_id>/<str:size>/', views.incident_photo, name='incident_photo'),
        path(S + 'cdm/', views.cdm_register, name='cdm_register'),
        path(S + 'cdm/edit/', views.cdm_edit, name='cdm_edit'),
        path(S + 'cdm/duty-holders/add/', views.duty_holder_add, name='duty_holder_add'),
        path(S + 'cdm/duty-holders/<int:holder_id>/', views.duty_holder_edit, name='duty_holder_edit'),
        path(S + 'cdm/plan/upload/', views.phase_plan_upload, name='phase_plan_upload'),
        path(S + 'cdm/plan/<int:plan_id>/', views.phase_plan_file, name='phase_plan_file'),
        path(S + 'on-site/', views.on_site, name='safety_on_site'),
        path(S + 'on-site/sign-in/', views.manual_sign_in, name='safety_manual_sign_in'),
        path(S + 'on-site/<int:attendance_id>/sign-out/', views.staff_sign_out, name='safety_sign_out'),
        path(S + 'sign-in-log/', views.sign_in_log, name='safety_sign_in_log'),
        path(S + 'roll-call/', views.roll_call, name='safety_roll_call'),
        path(S + 'roll-call/sw.js', views.roll_call_sw, name='safety_roll_call_sw'),
        path(S + 'poster/', views.poster, name='safety_poster'),
        path(S + 'poster/replace/', views.poster_replace, name='safety_poster_replace'),
        # An appointed subcontractor reports a near miss from their portal (safety/portal.py)
        path(S + 'near-miss/', portal.near_miss, name='portal_near_miss'),
    ]


org_patterns = [
    path('safety/incidents/', views.company_incidents, name='company_incidents'),
    path('safety/settings/', views.safety_settings, name='safety_settings'),
]

public_patterns = [
    path('sign-in/<str:token>/', public.site_signin, name='site_signin'),
    path('sign-in/<str:token>/out/', public.site_signout, name='site_signout'),
]
