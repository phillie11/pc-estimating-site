from django.urls import path

from . import views

urlpatterns = [
    path('search/', views.search_page, name='search'),
    path('help/', views.help_index, name='help_index'),
    path('help/<slug:slug>/', views.help_topic, name='help_topic'),
    path('getting-started/dismiss/', views.onboarding_dismiss, name='onboarding_dismiss'),
]
