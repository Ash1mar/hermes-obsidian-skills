"""Fixed stage budgets; historical pins remain authoritative after upgrades."""
import copy,json
from pathlib import Path
from .validation import ContractError,fingerprint

CONTRACT='stage-input-budget/v1'
PHASES=('candidate','citation','resource-reduce','global-plan','global-detail','page-write','page-pair','page-review','page-review-pair')

def fail(message):raise ContractError('INPUT_BUDGET_CHANGED','$',message)

def profile(name=None):
    config=json.loads(Path(__file__).with_name('input-budgets.json').read_text(encoding='utf-8'))
    name=name or config['default_profile']
    if name not in config['profiles']:fail('unknown input budget profile')
    row=copy.deepcopy(config['profiles'][name])
    row.update(contract=CONTRACT,profile=name,configuration_sha256=fingerprint(config))
    row['budget_sha256']=fingerprint(row)
    return validate(row)

def validate(row):
    if (not isinstance(row,dict) or row.get('contract')!=CONTRACT
            or row.get('budget_sha256')!=fingerprint({k:v for k,v in row.items() if k!='budget_sha256'})
            or set(row.get('phases',{}))!=set(PHASES)):fail('budget contract or fingerprint changed')
    for phase,p in row['phases'].items():
        if (type(p.get('input_codepoints')) is not int or p['input_codepoints'] not in (30000,40000)
                or type(p.get('output_tokens')) is not int or p['output_tokens']<1
                or type(p.get('output_reserve_tokens')) is not int or not 0<=p['output_reserve_tokens']<p['output_tokens']
                or p.get('input_tokens') is not None and (type(p['input_tokens']) is not int or p['input_tokens']<1)):
            fail('invalid phase input/output budget')
    return copy.deepcopy(row)

def from_value(value):
    row=value.get('backend',{}).get('input_budget')
    return validate(row) if row else None

def limit(value,phase,default=30000):
    row=from_value(value)
    return row['phases'][phase]['input_codepoints'] if row else default

def check_request(prepared,row,phase):
    row=validate(row);p=row['phases'][phase];cost=prepared['request_cost'];tokens=cost.get('text_tokens')
    reasons=[]
    if prepared['input_size']>p['input_codepoints']:reasons.append('complete_input_codepoints')
    if tokens is not None and p.get('input_tokens') is not None and tokens>p['input_tokens']:reasons.append('measured_text_input_tokens')
    context=p.get('context_tokens')
    if tokens is not None and context is not None and tokens+p['output_tokens']>context:reasons.append('configured_context_output_headroom')
    return {'budget_sha256':row['budget_sha256'],'phase':phase,'fits':not reasons,'split_reasons':reasons,
        'complete_request_codepoints':prepared['input_size'],'request_cost':copy.deepcopy(cost),
        'output_token_cap':p['output_tokens'],'usable_output_tokens':p['output_tokens']-p['output_reserve_tokens'],
        'context_headroom_tokens':context-tokens if context is not None and tokens is not None else None,
        'context_headroom_status':'measured_text_only' if context is not None and tokens is not None else 'unknown',
        'billable_input_tokens':None,'actual_usage_status':'not_called'}
