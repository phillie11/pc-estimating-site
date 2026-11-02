"""
The Quality tab's URLs. tenancy/urls_org.py splices these in with its project prefix, so they stay
plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path

from .views import common, firestop, inspections, permits


def project_patterns(P):
    Q = P + 'quality/'
    return [
        path(Q, inspections.inspection_list, name='inspection_list'),
        path(Q + 'inspections/new/', inspections.inspection_create, name='inspection_create'),
        path(Q + 'inspections/<int:ins_id>/', inspections.inspection_detail, name='inspection_detail'),
        path(Q + 'inspections/<int:ins_id>/action/', inspections.inspection_action, name='inspection_action'),
        path(Q + 'inspections/<int:ins_id>/checks/<int:item_id>/', inspections.inspection_item,
             name='inspection_item'),
        path(Q + 'inspections/<int:ins_id>/checks/<int:item_id>/snag/', inspections.inspection_snag,
             name='inspection_snag'),
        path(Q + 'permits/', permits.permit_list, name='permit_list'),
        path(Q + 'permits/new/', permits.permit_create, name='permit_create'),
        path(Q + 'permits/<int:permit_id>/', permits.permit_detail, name='permit_detail'),
        path(Q + 'permits/<int:permit_id>/edit/', permits.permit_edit, name='permit_edit'),
        path(Q + 'permits/<int:permit_id>/action/', permits.permit_action, name='permit_action'),
        path(Q + 'permits/<int:permit_id>/print/', permits.permit_print, name='permit_print'),
        path(Q + 'fire-stopping/', firestop.penetration_list, name='penetration_list'),
        path(Q + 'fire-stopping/new/', firestop.penetration_create, name='penetration_create'),
        path(Q + 'fire-stopping/<int:pen_id>/', firestop.penetration_detail, name='penetration_detail'),
        path(Q + 'fire-stopping/<int:pen_id>/edit/', firestop.penetration_edit, name='penetration_edit'),
        path(Q + 'fire-stopping/<int:pen_id>/photos/', firestop.penetration_photo, name='penetration_photo'),
        path(Q + 'fire-stopping/<int:pen_id>/action/', firestop.penetration_action, name='penetration_action'),
        path(Q + 'photos/<int:photo_id>/remove/', common.photo_remove, name='quality_photo_remove'),
        path(Q + 'photos/<int:photo_id>/<str:size>/', common.photo_file, name='quality_photo_file'),
    ]


org_patterns = [
    path('quality/checklists/', common.template_list, name='quality_templates'),
    path('quality/checklists/new/', common.template_create, name='quality_template_create'),
    path('quality/checklists/<int:template_id>/', common.template_edit, name='quality_template'),
    path('quality/settings/', common.quality_settings, name='quality_settings'),
]
