from django.urls import include, path

from actions import views as actions
from billing import views as billing
from changes import urls as changes_urls
from costs import urls as costs_urls
from delivery import views as delivery
from diary import urls as diary_urls
from directory import views as directory
from documents import urls as documents_urls
from levelling import urls as levelling_urls
from procurement import urls as procurement_urls
from projects import views as projects
from rfis import views as rfis
from tenders import views as tenders
from variations import views as variations

from . import views

P = 'projects/<int:pk>/'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('activity/', views.activity, name='activity'),
    path('notifications/', views.notifications, name='notifications'),
    path('notifications/<int:pk>/', views.notification_open, name='notification_open'),
    path('needs-me/', actions.needs_me, name='needs_me'),
    path('_ui/', actions.ui_kit, name='ui_kit'),
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
    path('bids/', projects.project_list, {'section': 'estimating'}, name='bid_list'),
    path('estimating/', views.estimating_home, name='estimating_home'),
    path('delivery/', views.delivery_home, name='delivery_home'),
    path('projects/new/', projects.project_create, name='project_create'),
    path(P, projects.project_detail, name='project_detail'),
    path(P + 'edit/', projects.project_edit, name='project_edit'),
    path(P + 'delete/', projects.project_delete, name='project_delete'),
    path(P + 'archive/', projects.project_archive, name='project_archive'),
    path(P + 'stage/', projects.project_stage, name='project_stage'),
    path(P + 'handover/', projects.project_handover, name='project_handover'),
    path(P + 'team/', projects.project_team, name='project_team'),
    path(P + 'decisions/', actions.project_decisions, name='project_decisions'),

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

    # Documents tab (documents/urls.py), listed flat so every project URL is a named pattern here
    *[path(P + 'documents/' + str(u.pattern), u.callback, name=u.name) for u in documents_urls.urlpatterns],

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
    path(P + 'variations/<int:vo_id>/evidence/', variations.variation_evidence, name='variation_evidence'),

    path(P + 'packages/', directory.package_list, name='package_list'),
    path(P + 'rams/', delivery.rams_list, name='rams_list'),
    path(P + 'rams/<int:rams_id>/', delivery.rams_detail, name='rams_detail'),
    path(P + 'rams/<int:rams_id>/file/', delivery.rams_file, name='rams_file'),
    path(P + 'sub-variations/', delivery.subvariation_list, name='subvariation_list'),
    path(P + 'sub-variations/<int:sv_id>/', delivery.subvariation_detail, name='subvariation_detail'),
    path(P + 'sub-variations/<int:sv_id>/file/', delivery.subvariation_file, name='subvariation_file'),
    path(P + 'tender/', tenders.tender, name='tender'),
    path(P + 'tender/addenda/<int:file_id>/', tenders.addendum_file, name='addendum_file'),
    path(P + 'tender/documents/<int:doc_id>/', tenders.tender_document, name='tender_document'),
    path(P + 'tender/quotes/<int:package_id>/', tenders.quote_file, name='quote_file'),

    path('directory/', directory.sub_list, name='sub_list'),
    path('directory/new/', directory.sub_create, name='sub_create'),
    path('directory/trades/', directory.trades, name='trades'),
    path('directory/<int:pk>/', directory.sub_detail, name='sub_detail'),
    path('directory/<int:pk>/edit/', directory.sub_edit, name='sub_edit'),
    path('directory/<int:pk>/archive/', directory.sub_archive, name='sub_archive'),

    # Commercial tab: contract set-up, change register, early warnings, change events (changes/urls.py)
    *changes_urls.project_patterns(P),
    *changes_urls.org_patterns,
    # Procurement tab: schedule and submittals (procurement/urls.py)
    *procurement_urls.project_patterns(P),
    # Diary tab: site diary, photos, delays, "Today on site" (diary/urls.py)
    *diary_urls.project_patterns(P),
    *diary_urls.org_patterns,
    # Commercial > Costs and Settings > Cost control (costs/urls.py)
    *costs_urls.project_patterns(P),
    *costs_urls.org_patterns,
    # Sub quotes > Level quotes on a bid (levelling/urls.py)
    *levelling_urls.project_patterns(P),
    path('', include('tenders.urls')),  # bid/no-bid gate, loss review, quote coverage (tenders/urls.py)
]
