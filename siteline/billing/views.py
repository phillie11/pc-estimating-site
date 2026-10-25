"""
Stripe subscriptions.

Flow:
  1. Admin picks a plan on /<org>/billing/ -> POST billing_checkout
  2. We create a Stripe Checkout Session (mode=subscription) tagged with the
     organisation id, and redirect to Stripe.
  3. Stripe calls /billing/webhook/ with checkout.session.completed and
     customer.subscription.* events; the webhook is the ONLY thing that
     changes an organisation's subscription status.
  4. "Manage billing" opens the Stripe Customer Portal (change plan, card,
     invoices, cancel).
"""
import json
import logging
from datetime import datetime, timezone as dt_tz

import stripe
from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from tenancy.models import Organization
from tenancy.permissions import admin_required

from .models import StripeEvent

logger = logging.getLogger(__name__)


def stripe_ready():
    return bool(settings.STRIPE_SECRET_KEY)


def _client():
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def billing(request, org_slug):
    org = request.org
    if not request.membership.is_admin:
        return render(request, 'tenancy/locked.html', {'state': org.access_state()}, status=402) \
            if not org.has_access else redirect('dashboard', org_slug=org_slug)
    plans = [{'code': code, **cfg} for code, cfg in settings.PLANS.items()]
    return render(request, 'billing/billing.html', {
        'plans': plans, 'stripe_ready': stripe_ready(), 'state': org.access_state(),
        'staff_count': org.staff_count(), 'project_count': org.active_project_count(),
        'subscribed': org.subscription_status in (Organization.STATUS_ACTIVE, Organization.STATUS_TRIALING,
                                                  Organization.STATUS_PAST_DUE),
    })


@require_POST
@admin_required
def checkout(request, org_slug):
    org = request.org
    code = request.POST.get('plan')
    plan = settings.PLANS.get(code)
    if not plan:
        return HttpResponseBadRequest('Unknown plan')
    if not stripe_ready() or not plan['stripe_price_id']:
        messages.error(request, 'Billing is not configured yet. Add your Stripe keys and price IDs to .env.')
        return redirect('billing', org_slug=org_slug)
    if org.stripe_subscription_id and org.subscription_status in (Organization.STATUS_ACTIVE, Organization.STATUS_PAST_DUE,
                                                                   Organization.STATUS_TRIALING):
        # Already subscribed: plan changes happen in the Customer Portal
        return portal(request, org_slug)
    s = _client()
    params = {
        'mode': 'subscription',
        'line_items': [{'price': plan['stripe_price_id'], 'quantity': 1}],
        'client_reference_id': str(org.pk),
        'metadata': {'org_id': str(org.pk), 'plan': code},
        'subscription_data': {'metadata': {'org_id': str(org.pk), 'plan': code}},
        'allow_promotion_codes': True,
        'billing_address_collection': 'required',
        'tax_id_collection': {'enabled': True},
        'success_url': settings.SITE_URL + reverse('billing_success', args=[org.slug]) + '?session_id={CHECKOUT_SESSION_ID}',
        'cancel_url': settings.SITE_URL + reverse('billing', args=[org.slug]),
    }
    if org.stripe_customer_id:
        params['customer'] = org.stripe_customer_id
        params['customer_update'] = {'address': 'auto', 'name': 'auto'}
    else:
        params['customer_email'] = request.user.email
    # Carry any unused internal trial across so people are not charged early
    if org.is_on_internal_trial and org.trial_ends_at and (org.trial_ends_at - timezone.now()).days >= 2:
        params['subscription_data']['trial_end'] = int(org.trial_ends_at.timestamp())
    try:
        session = s.checkout.Session.create(**params)
    except stripe.StripeError as e:
        logger.exception('Stripe checkout failed')
        messages.error(request, f'Stripe error: {getattr(e, "user_message", None) or "please try again"}')
        return redirect('billing', org_slug=org_slug)
    return redirect(session.url, permanent=False)


@admin_required
def success(request, org_slug):
    messages.success(request, 'Thanks, your subscription is being activated. This page updates once Stripe confirms payment.')
    return redirect('billing', org_slug=org_slug)


@admin_required
def portal(request, org_slug):
    org = request.org
    if not stripe_ready() or not org.stripe_customer_id:
        messages.error(request, 'No billing account yet. Choose a plan first.')
        return redirect('billing', org_slug=org_slug)
    try:
        session = _client().billing_portal.Session.create(
            customer=org.stripe_customer_id,
            return_url=settings.SITE_URL + reverse('billing', args=[org.slug]),
        )
    except stripe.StripeError:
        logger.exception('Stripe portal failed')
        messages.error(request, 'Could not open the billing portal. Check the Customer Portal is enabled in Stripe.')
        return redirect('billing', org_slug=org_slug)
    return redirect(session.url, permanent=False)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def _ts(value):
    return datetime.fromtimestamp(value, tz=dt_tz.utc) if value else None


def _plan_from_price(price_id):
    for code, cfg in settings.PLANS.items():
        if cfg['stripe_price_id'] and cfg['stripe_price_id'] == price_id:
            return code
    return None


def _find_org(obj):
    meta = obj.get('metadata') or {}
    org_id = meta.get('org_id') or obj.get('client_reference_id')
    if org_id:
        org = Organization.objects.filter(pk=org_id).first()
        if org:
            return org
    customer = obj.get('customer')
    if customer:
        return Organization.objects.filter(stripe_customer_id=customer).first()
    return None


def apply_subscription(org, sub):
    """Copy a Stripe Subscription object's state onto the organisation."""
    status = sub.get('status')
    org.stripe_subscription_id = sub.get('id', org.stripe_subscription_id)
    if sub.get('customer'):
        org.stripe_customer_id = sub['customer']
    if status in dict(Organization.SUBSCRIPTION_STATUS_CHOICES):
        org.subscription_status = status
    items = (sub.get('items') or {}).get('data') or []
    if items:
        price_id = (items[0].get('price') or {}).get('id')
        plan = _plan_from_price(price_id) or (sub.get('metadata') or {}).get('plan')
        if plan in settings.PLANS:
            org.plan = plan
    # current_period_end moved onto subscription items in newer Stripe API versions
    period_end = sub.get('current_period_end') or (items[0].get('current_period_end') if items else None)
    org.current_period_end = _ts(period_end)
    org.cancel_at_period_end = bool(sub.get('cancel_at_period_end'))
    if status == Organization.STATUS_ACTIVE:
        org.payment_failed_at = None
    elif status == Organization.STATUS_PAST_DUE and not org.payment_failed_at:
        org.payment_failed_at = timezone.now()
    org.save()


def handle_event(event):
    etype = event['type']
    obj = event['data']['object']
    org = _find_org(obj)
    if org is None:
        logger.warning('Stripe %s: no organisation matched', etype)
        return None

    if etype == 'checkout.session.completed':
        if obj.get('customer'):
            org.stripe_customer_id = obj['customer']
        if obj.get('subscription'):
            org.stripe_subscription_id = obj['subscription']
        org.save()
        if obj.get('subscription') and stripe_ready():
            try:
                apply_subscription(org, _client().Subscription.retrieve(obj['subscription']))
            except stripe.StripeError:
                logger.exception('Could not fetch subscription after checkout')
    elif etype in ('customer.subscription.created', 'customer.subscription.updated',
                   'customer.subscription.resumed', 'customer.subscription.paused'):
        is_current = not org.stripe_subscription_id or obj.get('id') == org.stripe_subscription_id
        # A different subscription only replaces the current one if it is the one being paid
        if is_current or obj.get('status') in (Organization.STATUS_ACTIVE, Organization.STATUS_TRIALING):
            apply_subscription(org, obj)
    elif etype == 'customer.subscription.deleted' and obj.get('id') not in (None, '', org.stripe_subscription_id):
        pass  # an old or abandoned subscription ended; the current one is unaffected
    elif etype == 'customer.subscription.deleted':
        org.subscription_status = Organization.STATUS_CANCELED
        org.cancel_at_period_end = False
        org.save()
    elif etype == 'invoice.payment_failed':
        if not org.payment_failed_at:
            org.payment_failed_at = timezone.now()
        if org.subscription_status == Organization.STATUS_ACTIVE:
            org.subscription_status = Organization.STATUS_PAST_DUE
        org.save()
    elif etype in ('invoice.paid', 'invoice.payment_succeeded'):
        org.payment_failed_at = None
        if org.subscription_status == Organization.STATUS_PAST_DUE:
            org.subscription_status = Organization.STATUS_ACTIVE
        org.save()
    return org


@csrf_exempt
@require_POST
def webhook(request):
    if not settings.STRIPE_WEBHOOK_SECRET:
        return HttpResponse('Webhook secret not configured', status=503)
    sig = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    try:
        stripe.Webhook.construct_event(request.body, sig, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        return HttpResponse(status=400)
    # Signature verified; work with plain dicts from here on
    event = json.loads(request.body)
    try:
        with transaction.atomic():
            record = StripeEvent.objects.create(event_id=event['id'], event_type=event['type'])
            org = handle_event(event)
            if org:
                record.organization = org
                record.save(update_fields=['organization'])
    except IntegrityError:
        return HttpResponse('duplicate')  # already processed
    return HttpResponse('ok')
