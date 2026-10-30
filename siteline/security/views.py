import io
import logging

import segno
from axes.handlers.proxy import AxesProxyHandler
from axes.helpers import get_lockout_response
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, user_login_failed
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from tenancy import notify
from tenancy.models import Membership, log_activity
from tenancy.permissions import admin_required, owner_required

from . import totp, twofactor
from .forms import CodeForm, StepUpForm, VerifyForm
from .models import RecoveryCode, TwoFactorDevice

log = logging.getLogger('security')

NEW_CODES_KEY = 'sl_new_recovery_codes'


def _safe_next(request, url, default='home'):
    if url and url_has_allowed_host_and_scheme(url, {request.get_host()}, request.is_secure()):
        return url
    return reverse(default)


def _failed(request, user):
    """Tell django-axes about a wrong code, so codes count towards the same lockout as passwords."""
    user_login_failed.send(sender=__name__, credentials={'username': user.email}, request=request)


def _no_store(response):
    response['Cache-Control'] = 'no-store'
    return response


# ---------------------------------------------------------------------------
# Sign-in, step two
# ---------------------------------------------------------------------------

@never_cache
def two_factor_verify(request):
    user = twofactor.pending_user(request)
    if user is None:
        messages.info(request, 'Your sign-in timed out. Enter your email and password again.')
        return redirect('login')
    device = twofactor.device_for(user)
    if device is None:  # two-factor was reset between the two steps: start again
        twofactor.clear_pending(request)
        return redirect('login')
    credentials = {'username': user.email}
    if not AxesProxyHandler.is_allowed(request, credentials):
        twofactor.clear_pending(request)
        return get_lockout_response(request, credentials)

    form = VerifyForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        used_recovery = False
        if form.cleaned_data['code']:
            ok = device.verify(form.cleaned_data['code'])
        else:
            ok = used_recovery = RecoveryCode.use(user, form.cleaned_data['recovery_code'])
        if ok:
            next_url = _safe_next(request, twofactor.pending_next(request))
            twofactor.clear_pending(request)
            login(request, user, backend=user.backend)
            response = redirect(next_url)
            if form.cleaned_data['remember']:
                twofactor.set_remember_cookie(response, user, device)
            if used_recovery:
                left = RecoveryCode.remaining(user)
                log.warning('User %s signed in with a recovery code (%s left)', user.pk, left)
                messages.warning(request, f'You used a recovery code. You have {left} left. '
                                          'If your phone is lost, set up two-factor sign-in again from Your account.')
            return response
        _failed(request, user)
        form.add_error(None, "That code didn't work. Check your phone's clock is set automatically and try the newest code.")
    return render(request, 'accounts/two_factor_verify.html', {'form': form, 'email': user.email})


# ---------------------------------------------------------------------------
# Setting it up
# ---------------------------------------------------------------------------

@never_cache
@login_required
def two_factor_setup(request):
    next_url = request.POST.get('next') or request.GET.get('next') or ''
    if twofactor.has_two_factor(request.user):
        messages.info(request, 'Two-factor sign-in is already on.')
        return redirect(_safe_next(request, next_url, 'profile'))
    device, _ = TwoFactorDevice.objects.get_or_create(user=request.user, defaults={'secret': totp.new_secret()})
    form = CodeForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        if device.verify(form.cleaned_data['code']):
            device.confirmed_at = timezone.now()
            device.save(update_fields=['confirmed_at'])
            request.session[NEW_CODES_KEY] = RecoveryCode.generate_for(request.user)
            twofactor.mark_authenticated(request)
            log.info('User %s turned on two-factor sign-in', request.user.pk)
            messages.success(request, 'Two-factor sign-in is on.')
            url = reverse('two_factor_codes')
            if next_url:
                from urllib.parse import urlencode
                url += '?' + urlencode({'next': next_url})
            return redirect(url)
        form.add_error('code', "That code didn't match. Type the newest 6-digit code your app shows.")
    return _no_store(render(request, 'accounts/two_factor_setup.html', {
        'form': form, 'secret': totp.grouped(device.secret), 'next': next_url,
        'required': twofactor.requires_two_factor(request.user),
    }))


@never_cache
@login_required
def two_factor_qr(request):
    """The set-up QR code, drawn here: the secret never leaves the server for a QR service."""
    device = TwoFactorDevice.objects.filter(user=request.user, confirmed_at__isnull=True).first()
    if device is None:
        raise Http404
    uri = totp.provisioning_uri(device.secret, request.user.email, settings.PRODUCT_NAME)
    buf = io.BytesIO()
    segno.make(uri, error='m').save(buf, kind='svg', scale=5, border=2, xmldecl=False, svgns=True,
                                    title='Scan with your authenticator app')
    return _no_store(HttpResponse(buf.getvalue(), content_type='image/svg+xml'))


@never_cache
@login_required
def two_factor_codes(request):
    codes = request.session.pop(NEW_CODES_KEY, None)
    if not codes:
        return redirect('profile')
    return _no_store(render(request, 'accounts/two_factor_codes.html', {
        'codes': codes, 'next': _safe_next(request, request.GET.get('next'))}))


@require_POST
@login_required
def two_factor_regenerate(request):
    if not twofactor.has_two_factor(request.user):
        return redirect('two_factor_setup')
    request.session[NEW_CODES_KEY] = RecoveryCode.generate_for(request.user)
    log.info('User %s made new recovery codes', request.user.pk)
    messages.success(request, 'New recovery codes made. The old ones no longer work.')
    return redirect('two_factor_codes')


@require_POST
@login_required
def two_factor_disable(request):
    if twofactor.requires_two_factor(request.user):
        messages.error(request, 'Owners and admins must keep two-factor sign-in on.')
        return redirect('profile')
    with transaction.atomic():
        TwoFactorDevice.objects.filter(user=request.user).delete()
        RecoveryCode.objects.filter(user=request.user).delete()
    log.info('User %s turned off two-factor sign-in', request.user.pk)
    messages.success(request, 'Two-factor sign-in is off.')
    response = redirect('profile')
    twofactor.forget_device(response)
    return response


# ---------------------------------------------------------------------------
# Step-up: confirm it's you
# ---------------------------------------------------------------------------

@never_cache
@login_required
def step_up(request):
    user = request.user
    device = twofactor.device_for(user)
    next_url = _safe_next(request, request.POST.get('next') or request.GET.get('next'))
    form = StepUpForm(request.POST or None, has_two_factor=device is not None)
    if request.method == 'POST' and form.is_valid():
        ok = False
        if form.cleaned_data.get('password'):
            # Through the auth backends, so django-axes counts a wrong password and honours a lockout
            ok = authenticate(request, username=user.email, password=form.cleaned_data['password']) == user
        elif device is not None:
            ok = device.verify(form.cleaned_data['code'])
            if not ok:
                _failed(request, user)
        if ok:
            twofactor.mark_authenticated(request)
            messages.success(request, "Thanks. That's confirmed for the next 10 minutes.")
            return redirect(next_url)
        form.add_error(None, "That didn't match. Try again.")
    return render(request, 'accounts/step_up.html', {'form': form, 'next': next_url})


# ---------------------------------------------------------------------------
# Owners: who has it on, and resetting someone's
# ---------------------------------------------------------------------------

@admin_required
def two_factor_team(request, org_slug):
    has_device = TwoFactorDevice.objects.filter(user=OuterRef('user'), confirmed_at__isnull=False)
    members = (request.org.memberships.filter(is_active=True).select_related('user')
               .annotate(has_two_factor=Exists(has_device)).order_by('user__full_name'))
    return render(request, 'security/two_factor_team.html', {'members': members})


@require_POST
@owner_required
def two_factor_reset_member(request, org_slug, pk):
    m = get_object_or_404(Membership, organization=request.org, pk=pk, is_active=True)
    target = m.user
    back = redirect('two_factor_team', org_slug=org_slug)
    if target == request.user:
        messages.error(request, 'To change your own two-factor sign-in, use Your account.')
        return back
    # An owner here mustn't weaken someone's sign-in to a different company's workspace
    if Membership.objects.filter(user=target, is_active=True).exclude(organization=request.org).exists():
        messages.error(request, f'{target.full_name} also works in another company\'s workspace, so only '
                                f'{settings.PRODUCT_NAME} support can reset their two-factor sign-in. '
                                f'Ask them to contact {settings.SUPPORT_EMAIL}.')
        return back
    with transaction.atomic():
        removed, _ = TwoFactorDevice.objects.filter(user=target).delete()
        RecoveryCode.objects.filter(user=target).delete()
        if removed:
            log_activity(request, 'reset two-factor sign-in for', target.full_name)
    if not removed:
        messages.info(request, f'{target.full_name} had not set up two-factor sign-in.')
        return back
    log.warning('Owner %s reset two-factor sign-in for user %s in organisation %s',
                request.user.pk, target.pk, request.org.pk)
    notify.send(target.email, 'Your two-factor sign-in was reset', 'two_factor_reset', {
        'user': target, 'actor': request.user, 'link': notify.absolute(reverse('login'))}, org=request.org)
    next_step = ('They will be asked to set it up again next time they sign in.' if twofactor.requires_two_factor(target)
                 else 'They can turn it back on from Your account.')
    messages.success(request, f'Two-factor sign-in reset for {target.full_name}. {next_step}')
    return back
