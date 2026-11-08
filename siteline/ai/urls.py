"""
AI drafting URLs. tenancy/urls_org.py splices these in with its project prefix, so they stay plain entries in
its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views, views_quotemap


def project_patterns(P):
    B = P + 'bid-brief/'
    Q = P + 'levelling/<int:trade_id>/ai-map/<int:package_id>/'
    return [
        path(Q, views_quotemap.quote_map, name='ai_quote_map'),
        path(Q + 'start/', views_quotemap.quote_map_start, name='ai_quote_map_start'),
        path(B, views.brief_page, name='ai_brief'),
        path(B + 'read/', views.brief_read, name='ai_brief_read'),
        path(B + 'rfis/', views.rfi_draft, name='ai_rfi_draft'),
        path(B + 'items/<int:item_id>/', views.brief_item, name='ai_brief_item'),
        path(B + 'items/<int:item_id>/accept/', views.brief_accept, name='ai_brief_accept'),
        path(P + 'handover-pack/clarifications/draft/', views.draft_clarifications, name='ai_draft_clarifications'),
    ]


org_patterns = [
    path('settings/ai/', views.settings_page, name='ai_settings'),
    path('settings/ai/usage/', views.usage_page, name='ai_usage'),
    path('settings/ai/playbooks/<slug:task>/', views.playbook_page, name='ai_playbook'),
]
