"""Versioned token-cost estimates; never a substitute for the provider invoice.

Verified 2026-11-25: https://developers.openai.com/api/docs/pricing
Unknown models/tiers/usage retain their original reservation. Images and search
tools retain their conservative reservation because token-only pricing is partial.
"""
from decimal import Decimal,ROUND_CEILING
import re

PRICING_VERSION='openai-text-2026-11-25'
SERPAPI_MODEL='serpapi:google_images'
PLAN_RESERVATION_USD=.10
RATES={'gpt-6-luna':('0.10','0.01','0.50'),
       'gpt-6-sol':('2.00','0.20','10.00'),
       'gpt-6.1-sol':('2.00','0.10','10.00'),
       'gpt-6.1-terra':('1.00','0.05','5.00')}


def token_cost_microusd(model,usage):
    if not isinstance(usage,dict):return None
    model=usage.get('_response_model') or model
    if not isinstance(model,str):return None
    model=re.sub(r'-\d{4}-\d{2}-\d{2}$','',model)
    if model not in RATES:return None
    tier=usage.get('_service_tier') or 'default'
    if tier not in {'default','standard','priority','flex'}:return None
    values=[usage.get('input_tokens'),usage.get('output_tokens')]
    details=usage.get('input_tokens_details') or {}
    if not isinstance(details,dict):return None
    if any(value for name,value in details.items() if 'write' in name or 'creation' in name):return None
    values.append(details.get('cached_tokens',0))
    if any(not isinstance(v,int) or isinstance(v,bool) or v<0 for v in values):return None
    input_tokens,output_tokens,cached=values
    if cached>input_tokens:return None
    uncached_rate,cached_rate,output_rate=map(Decimal,RATES[model])
    if input_tokens>272_000:
        uncached_rate*=2;cached_rate*=2;output_rate*=Decimal('1.5')
    multiplier={'priority':Decimal(2),'flex':Decimal('.5')}.get(tier,Decimal(1))
    # USD per million tokens is numerically equal to micro-USD per token.
    return int((((input_tokens-cached)*uncached_rate+cached*cached_rate+
                  output_tokens*output_rate)*multiplier).to_integral_value(rounding=ROUND_CEILING))


def accounted_microusd(call):
    if call['model']==SERPAPI_MODEL:return 0
    if call['status']=='completed' and call['stage']=='plan':
        frozen=call['usage_json'].get('_budget_cost',{})
        if (isinstance(frozen,dict) and isinstance(frozen.get('microusd'),int)
                and not isinstance(frozen['microusd'],bool) and frozen['microusd']>=0
                and frozen.get('pricing_version')):
            return frozen['microusd']
        cost=token_cost_microusd(call['model'],call['usage_json'])
        if cost is not None:return cost
    return call['reserved_microusd']
