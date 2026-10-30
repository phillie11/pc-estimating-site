import os

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path

from accounts import views as accounts
from actions import views as actions
from billing import views as billing
from security import views as security

admin.site.site_header = 'Platform admin'
ADMIN_PATH = os.environ.get('ADMIN_PATH', 'platform-admin').strip('/')

urlpatterns = [
    path('', accounts.landing, name='landing'),
    path('health/', lambda r: HttpResponse('ok'), name='health'),
    # The platform admin signs in through the main login, so two-factor applies there too
    path(f'{ADMIN_PATH}/login/', accounts.admin_login, name='admin_login_redirect'),
    path(f'{ADMIN_PATH}/', admin.site.urls),

    # Accounts
    path('signup/', accounts.signup, name='signup'),
    path('login/', accounts.LoginView.as_view(), name='login'),
    path('logout/', accounts.logout_view, name='logout'),
    path('login/code/', security.two_factor_verify, name='two_factor_verify'),
    path('home/', accounts.home, name='home'),
    path('orgs/', accounts.org_picker, name='org_picker'),
    path('account/', accounts.profile, name='profile'),
    path('account/round-up/', actions.digest_preference, name='digest_preference'),
    path('round-up/<str:token>/', actions.digest_unsubscribe, name='digest_unsubscribe'),
    path('account/password/', auth_views.PasswordChangeView.as_view(
        template_name='accounts/password_change.html', success_url='/account/'), name='password_change'),
    path('account/two-factor/', security.two_factor_setup, name='two_factor_setup'),
    path('account/two-factor/qr.svg', security.two_factor_qr, name='two_factor_qr'),
    path('account/two-factor/recovery-codes/', security.two_factor_codes, name='two_factor_codes'),
    path('account/two-factor/recovery-codes/new/', security.two_factor_regenerate, name='two_factor_regenerate'),
    path('account/two-factor/turn-off/', security.two_factor_disable, name='two_factor_disable'),
    path('account/confirm/', security.step_up, name='step_up'),
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

    # Two-factor status of the team, and an owner resetting someone's (TenantMiddleware applies)
    path('<slug:org_slug>/team/two-factor/', security.two_factor_team, name='two_factor_team'),
    path('<slug:org_slug>/team/two-factor/<int:pk>/reset/', security.two_factor_reset_member,
         name='two_factor_reset_member'),

    # Everything inside a company workspace. TenantMiddleware guards this.
    path('<slug:org_slug>/', include('tenancy.urls_org')),
]

handler404 = 'tenancy.errors.not_found'
handler403 = 'tenancy.errors.forbidden'
