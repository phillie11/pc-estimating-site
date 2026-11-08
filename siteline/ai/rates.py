"""
What each model costs, as data: US dollars per million tokens on Anthropic's own API (first-party list prices,
checked against Anthropic's model table on 6 October 2026). Usage is turned into pounds with settings.AI_GBP_PER_USD.

    input        uncached input tokens
    output       output tokens (thinking included: it is billed as output)
    cache_write  input written to the prompt cache (5-minute entries: 1.25 x input)
    cache_read   input read back from the cache
    long         optional: a dearer tier for a request whose prompt (input + cache write + cache read) is over
                 `above` tokens. The whole request is then charged at the long tier's rates. Claude Haiku 5.5 has
                 one: $0.10 / $0.50 up to 100K prompt tokens, $0.50 / $2.50 beyond.

When Anthropic changes a price, change it here; past usage rows keep the cost worked out when they were made.
A model missing from the table (say, a refusal fallback to a model added later) is costed at the dearest rates
in the table, so the monthly cap errs on the side of stopping early rather than late.
"""
from decimal import Decimal

D = Decimal


def _r(i, o, w, c, long=None):
    row = {'input': D(i), 'output': D(o), 'cache_write': D(w), 'cache_read': D(c)}
    if long:
        row['long'] = long
    return row


RATES_USD_PER_MTOK = {
    'claude-opus-5-5': _r('4.00', '20.00', '5.00', '0.20'),
    'claude-opus-5': _r('5.00', '25.00', '6.25', '0.50'),
    'claude-opus-4-8': _r('5.00', '25.00', '6.25', '0.50'),
    'claude-sonnet-5-5': _r('2.00', '10.00', '2.50', '0.20'),
    'claude-haiku-5-5': _r('0.10', '0.50', '0.125', '0.01',
                           long={'above': 100_000, **_r('0.50', '2.50', '0.625', '0.05')}),
}
MILLION = D('1000000')
KINDS = ('input', 'output', 'cache_write', 'cache_read')


def _tiers(row):
    return [row] + ([row['long']] if 'long' in row else [])


def dearest():
    """The highest rate of each kind across every model and tier: what an unknown model is costed at."""
    tiers = [t for row in RATES_USD_PER_MTOK.values() for t in _tiers(row)]
    return {k: max(t[k] for t in tiers) for k in KINDS}


def rate(model, prompt_tokens=0):
    row = RATES_USD_PER_MTOK.get(model)
    if row is None:
        return dearest()
    long = row.get('long')
    return long if long and prompt_tokens > long['above'] else row


def gbp_per_usd():
    from django.conf import settings
    return D(str(settings.AI_GBP_PER_USD))


def cost_usd(model, input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0):
    r = rate(model, prompt_tokens=input_tokens + cache_write_tokens + cache_read_tokens)
    return (D(input_tokens) * r['input'] + D(output_tokens) * r['output'] + D(cache_write_tokens) * r['cache_write']
            + D(cache_read_tokens) * r['cache_read']) / MILLION


def cost_gbp(model, input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0):
    usd = cost_usd(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
    return (usd * gbp_per_usd()).quantize(D('0.0001'))


def dearest_cost_gbp(input_tokens=0, output_tokens=0):
    """The most these tokens could cost on any model the request might run on (for holding against the budget)."""
    r = dearest()
    usd = (D(input_tokens) * r['input'] + D(output_tokens) * r['output']) / MILLION
    return (usd * gbp_per_usd()).quantize(D('0.0001'))
