"""
The Diary tab's URLs. tenancy/urls_org.py splices these in with its project prefix, so they stay
plain entries in its urlpatterns (the archived-project test walks that list).
"""
from django.urls import path, register_converter

from . import views


class IsoDate:
    regex = r'[0-9-]{1,10}'  # loose on purpose: views parse it, and anything but YYYY-MM-DD is a 404

    def to_python(self, value):
        return value  # views parse it, so a bad date is a 404 rather than a server error

    def to_url(self, value):
        return value.isoformat() if hasattr(value, 'isoformat') else str(value)


register_converter(IsoDate, 'isodate')


def project_patterns(P):
    D = P + 'diary/'
    return [
        path(D, views.diary_register, name='diary_register'),
        path(D + 'today/', views.diary_today, name='diary_today'),
        path(D + 'delays/', views.delays, name='diary_delays'),
        path(D + 'photos/', views.gallery, name='diary_gallery'),
        path(D + 'photos/upload/', views.photo_upload, name='diary_photo_upload'),
        path(D + 'photos/<int:photo_id>/', views.photo_edit, name='diary_photo'),
        path(D + 'photos/<int:photo_id>/<str:size>/', views.photo_file, name='diary_photo_file'),
        path(D + '<isodate:day>/', views.diary_day, name='diary_day'),
        path(D + '<isodate:day>/edit/', views.diary_edit, name='diary_edit'),
        path(D + '<isodate:day>/add/<str:kind>/', views.line_add, name='diary_line_add'),
        path(D + '<isodate:day>/remove/<str:kind>/<int:line_id>/', views.line_remove, name='diary_line_remove'),
        path(D + '<isodate:day>/lock/', views.diary_lock, name='diary_lock'),
        path(D + '<isodate:day>/pdf/', views.diary_pdf, name='diary_pdf'),
    ]


org_patterns = [
    path('diary/settings/', views.diary_settings, name='diary_settings'),
]
