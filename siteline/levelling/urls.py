"""
Quote levelling URLs. tenancy/urls_org.py splices these in with its project prefix, so they stay
plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views


def project_patterns(prefix):
    L = prefix + 'levelling/'
    T = L + '<int:trade_id>/'
    return [
        path(L, views.levelling_index, name='levelling_index'),
        path(T, views.levelling_sheet, name='levelling_sheet'),
        path(T + 'rates/<int:package_id>/', views.levelling_rates, name='levelling_rates'),
        path(T + 'import/', views.levelling_import, name='levelling_import'),
        path(T + 'levelling.csv', views.levelling_csv, name='levelling_csv'),
        path(T + 'levelling.pdf', views.levelling_pdf, name='levelling_pdf'),
    ]
