"""
What each model costs, as data: US dollars per million tokens on Anthropic's own API (first-party list prices,
checked against Anthropic's model table on 6 October 2026). Usage is turned into pounds with settings.AI_GBP_PER_USD.

    input        uncached input tokens
    output       output tokens (thinking included: it is billed as output)
    cache_write  input written to the prompt cache (5-minute entries: 1.25 x input)
    cache_read   input read back from the cache

When Anthropic changes a price, change it here; past usage rows keep the cost worked out when they were made.
A model missing from the table (say, a refusal fallback to a model added later) is costed at the dearest row,
so the monthly cap errs on the side of stopping early rather than late.
"""
from decimal import Decimal

D = Decimal

RATES_USD_PER_MTOK = {
    'claude-opus-5-5': {'input': D('4.00'), 'output': D('20.00'), 'cache_write': D('5.00'), 'cache_read': D('0.20')},
    'claude-opus-5': {'input': D('5.00'), 'output': D('25.00'), 'cache_write': D('6.25'), 'cache_read': D('0.50')},
    'claude-opus-4-8': {'input': D('5.00'), 'output': D('25.00'), 'cache_write': D('6.25'), 'cache_read': D('0.50')},
    'claude-sonnet-5-5': {'input': D('2.00'), 'output': D('10.00'), 'cache_write': D('2.50'), 'cache_read': D('0.20')},
    'claude-haiku-5-5': {'input': D('0.10'), 'output': D('0.50'), 'cache_write': D('0.125'), 'cache_read': D('0.01')},
}
MILLION = D('1000000')


def rate(model):
    if model in RATES_USD_PER_MTOK:
        return RATES_USD_PER_MTOK[model]
    return max(RATES_USD_PER_MTOK.values(), key=lambda r: r['output'])


def gbp_per_usd():
    from django.conf import settings
    return D(str(settings.AI_GBP_PER_USD))


def cost_gbp(model, input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0):
    r = rate(model)
    usd = (D(input_tokens) * r['input'] + D(output_tokens) * r['output'] + D(cache_write_tokens) * r['cache_write']
           + D(cache_read_tokens) * r['cache_read']) / MILLION
    return (usd * gbp_per_usd()).quantize(D('0.0001'))
