import os

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path

from accounts import views as accounts
from billing import views as billing

admin.site.site_header = 'Platform admin'
ADMIN_PATH = os.environ.get('ADMIN_PATH', 'platform-admin').strip('/')

urlpatterns = [
    path('', accounts.landing, name='landing'),
    path('health/', lambda r: HttpResponse('ok'), name='health'),
    path(f'{ADMIN_PATH}/', admin.site.urls),

    # Accounts
    path('signup/', accounts.signup, name='signup'),
    path('login/', accounts.LoginView.as_view(), name='login'),
    path('logout/', accounts.logout_view, name='logout'),
    path('home/', accounts.home, name='home'),
    path('orgs/', accounts.org_picker, name='org_picker'),
    path('account/', accounts.profile, name='profile'),
    path('account/password/', auth_views.PasswordChangeView.as_view(
        template_name='accounts/password_change.html', success_url='/account/'), name='password_change'),
    path('password-reset/', auth_views.PasswordResetView.as_view(
        template_name='accounts/password_reset.html', email_template_name='email/password_reset.txt',
        subject_template_name='email/password_reset_subject.txt'), name='password_reset'),
    path('password-reset/sent/', auth_views.PasswordResetDoneView.as_view(
        template_name='accounts/password_reset_done.html'), name='password_reset_done'),
    path('password-reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='accounts/password_reset_confirm.html'), name='password_reset_confirm'),
    path('password-reset/done/', auth_views.PasswordResetCompleteView.as_view(
        template_name='accounts/password_reset_complete.html'), name='password_reset_complete'),

    # Invites (public links; the token is the credential)
    path('invite/team/<str:token>/', accounts.accept_team_invite, name='accept_team_invite'),
    path('invite/project/<str:token>/', accounts.accept_project_invite, name='accept_project_invite'),

    # Stripe
    path('billing/webhook/', billing.webhook, name='stripe_webhook'),

    # Everything inside a company workspace. TenantMiddleware guards this.
    path('<slug:org_slug>/', include('tenancy.urls_org')),
]

handler404 = 'tenancy.errors.not_found'
handler403 = 'tenancy.errors.forbidden'
