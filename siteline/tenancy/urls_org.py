from django.urls import path

from billing import views as billing
from directory import views as directory
from projects import views as projects
from rfis import views as rfis
from variations import views as variations

from . import views

P = 'projects/<int:pk>/'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('activity/', views.activity, name='activity'),
    path('team/', views.team, name='team'),
    path('team/<int:pk>/', views.member_update, name='member_update'),
    path('team/invites/<int:pk>/revoke/', views.invite_revoke, name='invite_revoke'),
    path('settings/', views.org_settings, name='org_settings'),
    path('logo/', views.org_logo, name='org_logo'),
    path('locked/', views.org_locked, name='org_locked'),

    path('billing/', billing.billing, name='billing'),
    path('billing/checkout/', billing.checkout, name='billing_checkout'),
    path('billing/portal/', billing.portal, name='billing_portal'),
    path('billing/success/', billing.success, name='billing_success'),

    path('pipeline/', projects.pipeline, name='pipeline'),
    path('analytics/', projects.tender_analytics, name='tender_analytics'),
    path('workload/', projects.workload, name='workload'),
    path('rfis/', rfis.rfi_register, name='rfi_register'),

    path('projects/', projects.project_list, name='project_list'),
    path('projects/new/', projects.project_create, name='project_create'),
    path(P, projects.project_detail, name='project_detail'),
    path(P + 'edit/', projects.project_edit, name='project_edit'),
    path(P + 'delete/', projects.project_delete, name='project_delete'),
    path(P + 'stage/', projects.project_stage, name='project_stage'),
    path(P + 'team/', projects.project_team, name='project_team'),

    path(P + 'programme/', projects.programme, name='programme'),
    path(P + 'programme/<int:activity_id>/', projects.activity_edit, name='activity_edit'),

    path(P + 'tasks/', projects.task_list, name='task_list'),
    path(P + 'tasks/<int:task_id>/update/', projects.task_update, name='task_update'),
    path(P + 'tasks/<int:task_id>/edit/', projects.task_edit, name='task_edit'),

    path(P + 'issues/', projects.issue_list, name='issue_list'),
    path(P + 'issues/new/', projects.issue_create, name='issue_create'),
    path(P + 'issues/<int:issue_id>/', projects.issue_detail, name='issue_detail'),
    path(P + 'issues/<int:issue_id>/edit/', projects.issue_edit, name='issue_edit'),
    path(P + 'issues/<int:issue_id>/photo/', projects.issue_photo, name='issue_photo'),

    path(P + 'documents/', projects.document_list, name='document_list'),
    path(P + 'documents/<int:doc_id>/', projects.document_detail, name='document_detail'),
    path(P + 'documents/<int:doc_id>/rev/<int:rev_id>/', projects.document_download, name='document_download'),

    path(P + 'rfis/', rfis.rfi_list, name='rfi_list'),
    path(P + 'rfis/new/', rfis.rfi_create, name='rfi_create'),
    path(P + 'rfis/<int:rfi_id>/', rfis.rfi_detail, name='rfi_detail'),
    path(P + 'rfis/<int:rfi_id>/edit/', rfis.rfi_edit, name='rfi_edit'),
    path(P + 'rfis/<int:rfi_id>/file/', rfis.rfi_attachment, name='rfi_attachment'),

    path(P + 'variations/', variations.variation_list, name='variation_list'),
    path(P + 'variations/new/', variations.variation_create, name='variation_create'),
    path(P + 'variations/<int:vo_id>/', variations.variation_detail, name='variation_detail'),
    path(P + 'variations/<int:vo_id>/edit/', variations.variation_edit, name='variation_edit'),
    path(P + 'variations/<int:vo_id>/file/', variations.variation_attachment, name='variation_attachment'),

    path(P + 'packages/', directory.package_list, name='package_list'),

    path('directory/', directory.sub_list, name='sub_list'),
    path('directory/new/', directory.sub_create, name='sub_create'),
    path('directory/trades/', directory.trades, name='trades'),
    path('directory/<int:pk>/', directory.sub_detail, name='sub_detail'),
    path('directory/<int:pk>/edit/', directory.sub_edit, name='sub_edit'),
]
