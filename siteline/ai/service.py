"""
The one way Siteline calls the AI model. Every feature goes through call(), which:

  1. checks, holding the company's AISettings row locked, that AI drafting is on, that this person may use it,
     and that this month's spend plus the most this call could cost stays inside the cap. Anything missing or
     unclear refuses (fail closed). The most it could cost is reserved straight away as an AIUsage row, so two
     calls at once can't both squeeze under the cap;
  2. calls the model with a timeout and the SDK's limited retries (settings.AI_TIMEOUT_SECONDS,
     AI_MAX_RETRIES), streaming, with structured output so the reply is JSON of a fixed shape;
  3. writes what the call really cost (tokens, cached tokens, model, pounds) over the reservation, and logs
     who, what and when. Prompt and reply text are never stored or logged here: a feature keeps only the
     draft it saves for a person to edit.

The API key comes from the ANTHROPIC_API_KEY environment variable and nowhere else: it is not a setting, not
stored in the database, and not logged. Without it every feature says it isn't set up and nothing is sent.

Calls run in the worker (`manage.py run_worker`), never in a web request: see ai/tasks.py.
Tests replace get_client() with a fake; nothing in the test suite reaches the network.
"""
import json
import logging
import os
from datetime import datetime, time
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from . import rates
from .models import AISettings, AIUsage

logger = logging.getLogger(__name__)

FALLBACK_BETA = 'server-side-fallback-2026-07-01'
# Reserving against the budget: a PDF page is 1,500 to 3,000 text tokens plus the page image (Anthropic's PDF
# guide), so 5,000 a page over-reserves rather than under; text is about 4 characters a token, so 3 over-reserves.
TOKENS_PER_PAGE = 5000
CHARS_PER_TOKEN = 3


class Unavailable(Exception):
    """AI drafting can't be used here and now. The message is for the person, in plain words."""


class CallFailed(Exception):
    """The call was made but didn't give a usable draft. The message is for the person."""


def server_ready():
    return bool(os.environ.get('ANTHROPIC_API_KEY', '').strip())


def get_client():
    import anthropic
    return anthropic.Anthropic(
        api_key=os.environ.get('ANTHROPIC_API_KEY', '').strip(),
        timeout=anthropic.Timeout(float(settings.AI_TIMEOUT_SECONDS), connect=10.0),
        max_retries=int(settings.AI_MAX_RETRIES))


def month_start(now=None):
    now = timezone.localtime(now or timezone.now())
    return timezone.make_aware(datetime.combine(now.date().replace(day=1), time.min))


def month_spend(org):
    """Pounds spent (or held for calls in progress) this calendar month."""
    total = AIUsage.objects.filter(organization=org, created_at__gte=month_start()).aggregate(t=Sum('cost_gbp'))['t']
    return (total or Decimal('0')).quantize(Decimal('0.01'))


def may_use(membership, ai):
    if membership is None or not membership.is_active or not membership.is_internal:
        return False
    if ai.who == AISettings.ADMINS:
        return membership.is_admin
    if ai.who == AISettings.ESTIMATORS:
        return membership.can_estimate
    return ai.who == AISettings.STAFF


def status(org, membership, ai=None):
    """(usable, reason): whether this person can start a draft now, and if not, why, in plain words."""
    if membership is None or not membership.is_internal:
        return False, ''
    ai = ai or getattr(org, '_ai_settings', None) or AISettings.for_org(org)
    if not server_ready():
        return False, 'AI drafting isn\'t set up on this Siteline server yet.'
    if not ai.enabled:
        return False, ('AI drafting is switched off for your company. An owner or admin can switch it on under '
                       'Settings > AI drafting.')
    if not may_use(membership, ai):
        return False, f'Your company has AI drafting for {ai.get_who_display().lower()}.'
    if month_spend(org) >= ai.monthly_cap:
        return False, (f'This month\'s AI budget of £{ai.monthly_cap:,.2f} has been used. It starts again on the 1st, '
                       'or an owner or admin can raise it.')
    return True, ''


def estimate(model, max_tokens, chars=0, pages=0):
    """The most a call could cost, in pounds: every output token used, and a generous count of input tokens."""
    tokens_in = chars // CHARS_PER_TOKEN + pages * TOKENS_PER_PAGE + 2000
    return rates.cost_gbp(model, input_tokens=tokens_in, output_tokens=max_tokens)


def _reserve(org, user, feature, project, run, model, held, note):
    from tenancy.models import Membership
    with transaction.atomic():
        ai = AISettings.objects.select_for_update().filter(organization=org).first()
        if ai is None or not ai.enabled:
            raise Unavailable('AI drafting is switched off for your company.')
        if not server_ready():
            raise Unavailable('AI drafting isn\'t set up on this Siteline server yet.')
        membership = Membership.objects.filter(organization=org, user=user, is_active=True).first() if user else None
        if not may_use(membership, ai):
            raise Unavailable('You can\'t use AI drafting: ask an owner or admin.')
        spent = month_spend(org)
        if spent + held > ai.monthly_cap:
            left = max(ai.monthly_cap - spent, Decimal('0'))
            raise Unavailable(f'Not enough of this month\'s AI budget is left (£{left:,.2f} of £{ai.monthly_cap:,.2f}) '
                              f'for this, which could cost up to £{held:,.2f}. An owner or admin can raise the budget '
                              'under Settings > AI drafting.')
        return AIUsage.objects.create(organization=org, user=user, feature=feature, project=project, run=run,
                                      model=model, cost_gbp=held, note=note[:200])


def _text(message):
    return ''.join(getattr(b, 'text', '') for b in message.content if getattr(b, 'type', '') == 'text')


def call(*, org, user, task, project, run, content, chars=0, pages=0, note='', effort=None):
    """
    Ask the model to do one registered task (ai/registry.py) on `content`, the inputs the feature built, and
    return its structured reply as a dict (not yet validated: the feature checks it). The system prompt is the
    task's fixed rules plus the company's playbook (registry.system_prompt). Raises Unavailable before anything
    is sent, or CallFailed after.
    """
    import anthropic

    from . import registry
    t = registry.get(task)
    feature, schema, max_tokens = t.feature, t.schema, t.max_tokens
    system = registry.system_prompt(org, task)
    chars += len(system)
    model = settings.AI_MODEL
    usage = _reserve(org, user, feature, project, run, model, estimate(model, max_tokens, chars, pages), note)
    params = {
        'model': model, 'max_tokens': max_tokens,
        # The rules and playbook are the same for every call of this task in this company, so they are cached
        # and a second call within a few minutes reads them back at a fraction of the price
        'system': [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
        'messages': [{'role': 'user', 'content': content}],
        'output_config': {'effort': effort or settings.AI_EFFORT,
                          'format': {'type': 'json_schema', 'schema': schema}},
    }
    if settings.AI_REFUSAL_FALLBACK:
        # If the model's safety checks decline a request, the API re-runs it on Anthropic's recommended fallback
        params.update(betas=[FALLBACK_BETA], fallbacks='default')
    message = None
    try:
        with get_client().beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
    except anthropic.RateLimitError:
        _finish(usage, None, AIUsage.FAILED)
        raise CallFailed('The AI service is busy. Try again in a few minutes.')
    except anthropic.APITimeoutError:
        _finish(usage, None, AIUsage.FAILED)
        raise CallFailed('The AI service took too long to answer. Try again, or with fewer pages.')
    except anthropic.AuthenticationError:
        _finish(usage, None, AIUsage.FAILED)
        logger.error('ai: the AI service refused the API key (feature %s, company %s)', feature, org.pk)
        raise CallFailed('The AI service refused this server\'s key. Tell whoever runs your Siteline server.')
    except anthropic.APIStatusError as e:
        _finish(usage, None, AIUsage.FAILED)
        logger.warning('ai: call failed with HTTP %s (feature %s, company %s, request %s)', e.status_code, feature,
                       org.pk, getattr(e, 'request_id', ''))
        if e.status_code == 413:
            raise CallFailed('That is too much to send in one go. Choose fewer or smaller documents.')
        raise CallFailed('The AI service couldn\'t do this just now. Try again later.')
    except anthropic.APIConnectionError:
        _finish(usage, None, AIUsage.FAILED)
        raise CallFailed('Siteline couldn\'t reach the AI service. Try again later.')

    stop = getattr(message, 'stop_reason', None)
    _finish(usage, message, AIUsage.REFUSED if stop == 'refusal' else AIUsage.OK)
    if stop == 'refusal':
        raise CallFailed('The AI model declined to draft this. Nothing was saved.')
    if stop == 'max_tokens':
        raise CallFailed('The draft was too long to finish. Try again with less.')
    try:
        data = json.loads(_text(message))
    except (TypeError, ValueError):
        raise CallFailed('The AI model\'s answer wasn\'t in the expected shape. Nothing was saved.')
    if not isinstance(data, dict):
        raise CallFailed('The AI model\'s answer wasn\'t in the expected shape. Nothing was saved.')
    return data


def _finish(usage, message, status):
    u = getattr(message, 'usage', None)
    tokens = {
        'input_tokens': int(getattr(u, 'input_tokens', 0) or 0),
        'output_tokens': int(getattr(u, 'output_tokens', 0) or 0),
        'cache_write_tokens': int(getattr(u, 'cache_creation_input_tokens', 0) or 0),
        'cache_read_tokens': int(getattr(u, 'cache_read_input_tokens', 0) or 0),
    }
    model = str(getattr(message, 'model', '') or usage.model)[:60]
    usage.model, usage.status = model, status
    for k, v in tokens.items():
        setattr(usage, k, v)
    usage.cost_gbp = rates.cost_gbp(model, **tokens)
    usage.request_id = str(getattr(message, '_request_id', '') or getattr(message, 'id', '') or '')[:100]
    usage.save()
    logger.info('ai: %s for company %s by user %s on project %s: %s, model %s, %s in (%s cached) / %s out tokens, '
                '£%s', usage.feature, usage.organization_id, usage.user_id, usage.project_id, status, model,
                tokens['input_tokens'], tokens['cache_read_tokens'], tokens['output_tokens'], usage.cost_gbp)
