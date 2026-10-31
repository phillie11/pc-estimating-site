"""
Cost control URLs. tenancy/urls_org.py splices these in with its project prefix, so they stay
plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views


def project_patterns(P):
    C = P + 'commercial/costs/'
    return [
        path(C, views.report, name='cost_report'),
        path(C + 'forecast/', views.forecast, name='cost_forecast'),
        path(C + 'budget/', views.budget, name='cost_budget'),
        path(C + 'budget/new/', views.budget_setup, name='cost_budget_setup'),
        path(C + 'budget/change/', views.budget_change, name='cost_budget_change'),
        path(C + 'orders/', views.commitments, name='cost_commitments'),
        path(C + 'orders/commit-package/', views.commit_package, name='cost_commit_package'),
        path(C + 'orders/new/', views.order_create, name='cost_order_create'),
        path(C + 'orders/<int:commitment_id>/', views.commitment_detail, name='cost_commitment_detail'),
        path(C + 'actuals/', views.actuals, name='cost_actuals'),
        path(C + 'actuals/new/', views.actual_create, name='cost_actual_create'),
        path(C + 'actuals/<int:actual_id>/file/', views.actual_file, name='cost_actual_file'),
        path(C + 'ledger/', views.ledger_list, name='cost_ledger'),
        path(C + 'ledger/<int:entry_id>/reverse/', views.ledger_reverse, name='cost_ledger_reverse'),
    ]


org_patterns = [
    path('settings/costs/', views.cost_settings, name='cost_settings'),
    path('settings/costs/<int:code_id>/', views.cost_code_edit, name='cost_code_edit'),
]
