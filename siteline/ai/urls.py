"""
AI drafting URLs. tenancy/urls_org.py splices these in with its project prefix, so they stay plain entries in
its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views


def project_patterns(P):
    B = P + 'bid-brief/'
    return [
        path(B, views.brief_page, name='ai_brief'),
        path(B + 'read/', views.brief_read, name='ai_brief_read'),
        path(B + 'items/<int:item_id>/', views.brief_item, name='ai_brief_item'),
        path(B + 'items/<int:item_id>/accept/', views.brief_accept, name='ai_brief_accept'),
        path(P + 'handover-pack/clarifications/draft/', views.draft_clarifications, name='ai_draft_clarifications'),
    ]


org_patterns = [
    path('settings/ai/', views.settings_page, name='ai_settings'),
    path('settings/ai/usage/', views.usage_page, name='ai_usage'),
    path('settings/ai/playbooks/<slug:task>/', views.playbook_page, name='ai_playbook'),
]
