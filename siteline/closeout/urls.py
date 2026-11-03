"""
The Close-out tab's URLs. tenancy/urls_org.py splices these in with its project prefix, so they
stay plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from . import views


def project_patterns(P):
    C = P + 'close-out/'
    return [
        path(C, views.overview, name='closeout_overview'),
        path(C + 'sections/new/', views.section_add, name='closeout_section_add'),
        path(C + 'sections/<int:section_id>/certificate/', views.section_certificate,
             name='closeout_section_certificate'),
        path(C + 'sections/<str:section_id>/', views.section_edit, name='closeout_section'),
        path(C + 'defects/', views.defects, name='closeout_defects'),
        path(C + 'defects/new/', views.defect_new, name='closeout_defect_new'),
        path(C + 'om/', views.om, name='closeout_om'),
        path(C + 'om/sections/new/', views.om_section_new, name='closeout_om_section_new'),
        path(C + 'om/sections/<int:section_id>/', views.om_section, name='closeout_om_section'),
        path(C + 'om/sections/<int:section_id>/request/', views.om_request, name='closeout_om_request'),
        path(C + 'om/sections/<int:section_id>/upload/', views.om_upload, name='closeout_om_upload'),
        path(C + 'om/files/<int:doc_id>/', views.om_document, name='closeout_om_document'),
        path(C + 'om/manual/<int:comp_id>/', views.om_download, name='closeout_om_download'),
        path(C + 'certificates/', views.certificates, name='closeout_certificates'),
        path(C + 'certificates/<int:cert_id>/', views.certificate_file, name='closeout_certificate_file'),
        path(C + 'handover/', views.packs, name='closeout_packs'),
        path(C + 'handover/<int:pack_id>/', views.pack_detail, name='closeout_pack'),
        path(C + 'handover/<int:pack_id>/manifest.pdf', views.pack_manifest, name='closeout_pack_manifest'),
        path(C + 'handover/<int:pack_id>/items/<int:item_id>/', views.pack_item, name='closeout_pack_item'),
        path(P + 'defects-and-handover/', views.client_page, name='closeout_client'),
    ]


org_patterns = [
    path('close-out/om-template/', views.om_template, name='closeout_om_template'),
]
