"""
/api/v1/... (config/urls.py, outside any workspace: the token says which company) and Settings → API
(tenancy/urls_org.py splices org_patterns in, under TenantMiddleware like every workspace page).
"""
from django.urls import path

from . import views
from .resources import resources

urlpatterns = [
    path('', views.index, name='api_index'),
    path('openapi.json', views.openapi_json, name='api_openapi'),
    path('documents/<int:doc_id>/revisions/<int:rev_id>/download/', views.document_download,
         name='api_document_download'),
]
for _r in resources():
    urlpatterns.append(path(f'{_r.name}/', views.resource_list, {'name': _r.name}, name=_r.list_url_name()))
    if _r.detail:
        urlpatterns.append(path(f'{_r.name}/<int:pk>/', views.resource_detail, {'name': _r.name},
                                name=_r.detail_url_name()))

org_patterns = [
    path('settings/api/', views.api_settings, name='api_settings'),
    path('settings/api/how-to/', views.api_docs, name='api_docs'),
    path('settings/api/<int:token_id>/', views.api_token_detail, name='api_token_detail'),
    path('settings/api/<int:token_id>/revoke/', views.api_token_revoke, name='api_token_revoke'),
]
