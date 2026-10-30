"""Documents tab URLs, included under /<org>/projects/<pk>/documents/ by tenancy/urls_org.py."""
from django.urls import path

from . import views

urlpatterns = [
    path('', views.document_list, name='document_list'),
    path('<int:doc_id>/', views.document_detail, name='document_detail'),
    path('<int:doc_id>/rev/<int:rev_id>/', views.document_download, name='document_download'),
    path('<int:doc_id>/rev/<int:rev_id>/view/', views.revision_detail, name='revision_detail'),
    path('bulk/', views.bulk_upload, name='bulk_upload'),
    path('bulk/<int:batch_id>/', views.bulk_upload_batch, name='bulk_upload_batch'),
    path('transmittals/', views.transmittal_list, name='transmittal_list'),
    path('transmittals/new/', views.transmittal_create, name='transmittal_create'),
    path('transmittals/<int:tr_id>/', views.transmittal_detail, name='transmittal_detail'),
    path('transmittals/<int:tr_id>/edit/', views.transmittal_create, name='transmittal_edit'),
    path('transmittals/<int:tr_id>/sheet/', views.transmittal_sheet, name='transmittal_sheet'),
    path('settings/', views.register_settings, name='register_settings'),
]
