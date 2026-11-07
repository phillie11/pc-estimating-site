"""
Pricing workbook and handover pack URLs. tenancy/urls_org.py splices these in with its project prefix, so they
stay plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views


def project_patterns(P):
    W, H = P + 'pricing-workbook/', P + 'handover-pack/'
    return [
        path(W, views.workbook_page, name='hpack_workbook'),
        path(W + 'upload/', views.workbook_upload, name='hpack_upload'),
        path(W + '<int:rev_id>/', views.revision_detail, name='hpack_revision'),
        path(W + '<int:rev_id>/file/', views.revision_file, name='hpack_file'),
        path(H, views.pack_page, name='hpack_pack'),
        path(H + 'issue/', views.pack_issue, name='hpack_issue'),
        path(H + 'site-rules/', views.site_rules, name='hpack_site_rules'),
        path(H + 'clarifications/', views.clarifications, name='hpack_clarifications'),
        path(H + 'clarifications/<int:cl_id>/', views.clarification_edit, name='hpack_clarification'),
        path(H + 'open-items/', views.open_items, name='hpack_items'),
    ]


org_patterns = [
    path('settings/pricing-workbook/', views.settings_page, name='hpack_settings'),
]
