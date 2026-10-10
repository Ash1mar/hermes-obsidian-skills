"""Closed semantic outputs. IDs, revisions, leases and run assignment stay in code."""
import copy
from functools import lru_cache

from hermes_source_units.semantic_submission import closed, native_submission_schema
from hermes_source_units.validation import _check_shape, load_schema

TEXT = {'type':'string', 'minLength':1}
HANDLE = {'type':'string', 'pattern':'^c[1-9][0-9]*$'}
REFS = {'type':'array','items':HANDLE,'uniqueItems':True}


def definition(name):
    defs = load_schema()['$defs']
    def inline(value):
        if isinstance(value, list): return [inline(v) for v in value]
        if not isinstance(value, dict): return value
        if '$ref' in value: return inline(defs[value['$ref'].split('/')[-1]])
        return {k:inline(v) for k,v in value.items()}
    return inline(defs[name])


@lru_cache(maxsize=8)
def _schema(phase):
    draft = native_submission_schema()['properties']['passes']['items']
    for name in ('input_id','sequence'):
        draft['properties'].pop(name)
        if name in draft['required']: draft['required'].remove(name)
    if phase == 'candidate':
        result = closed({'drafts':{'type':'array','items':draft,'minItems':1}})
    elif phase == 'citation':
        confirmation = closed({'task':draft['properties']['task'],
            'decision':{'const':'confirmed_unchanged'},'review_note':TEXT})
        rewritten = copy.deepcopy(draft)
        rewritten['properties']['removed_candidate_ids']={'type':'array','items':TEXT,'uniqueItems':True}
        rewritten['required'].append('removed_candidate_ids')
        rewritten['properties']['inspections'].pop('minItems',None)
        rewritten['properties'].update(decision={'const':'revised'},review_note=TEXT)
        rewritten['required'].extend(('decision','review_note'))
        result = closed({'reviews':{'type':'array','items':{'oneOf':[confirmation,rewritten]},'minItems':1}})
    elif phase == 'resource-reduce':
        proposal = definition('resource_proposal')
        proposal['properties'].pop('proposal_id'); proposal['required'].remove('proposal_id')
        proposal['properties']['candidate_refs'] = {**REFS,'minItems':1}
        result = closed({'proposals':{'type':'array','items':proposal},
            'omitted_candidate_refs':REFS,'reason':TEXT})
    elif phase in ('global-reduce','global-plan'):
        page = closed({'candidate_refs':{**REFS,'minItems':1},
            'identity':definition('resource_identity_hint'),
            'path':{'type':'string','minLength':1,'format':'vault-relative-path'},'content':TEXT})
        if phase=='global-plan':
            page['properties'].pop('content'); page['required'].remove('content')
        result = closed({'pages':{'type':'array','items':page},'omitted_candidate_refs':REFS,'reason':TEXT})
    elif phase=='page-write':
        result=closed({'content':TEXT})
    elif phase=='page-review':
        result=closed({'decision':{'type':'string','enum':['approved','revision_required']},'review_note':TEXT})
    else: raise ValueError('unknown fixed semantic phase')
    return {'oneOf':[result,closed({'blocked_reason':TEXT})]}


def schema(phase):
    return copy.deepcopy(_schema(phase))

def validate(phase, value):
    _check_shape(schema(phase),value,{},'$')


def slot_schema(phase, tasks):
    """The program fixes the return keys; the model never repeats task IDs."""
    key = 'drafts' if phase == 'candidate' else 'reviews'
    item = schema(phase)['oneOf'][0]['properties'][key]['items']
    branches = item.get('oneOf', [item])
    for branch in branches:
        branch['properties'].pop('task')
        branch['required'].remove('task')
    return {**closed({'results':closed({t['task']:{'$ref':'#/$defs/result'} for t in tasks})}),
            '$defs':{'result':{'anyOf':[*branches,closed({'blocked_reason':TEXT})]}}}


def strict_schema(value):
    """Transport subset only; full canonical constraints remain local checks."""
    if isinstance(value, list): return [strict_schema(v) for v in value]
    if not isinstance(value, dict): return value
    result={('anyOf' if k=='oneOf' else k):strict_schema(v) for k,v in value.items()
            if k not in ('uniqueItems','format','minLength','maxLength')}
    if 'const' in result:
        constant=result.pop('const');result['enum']=[constant]
    if 'enum' in result and 'type' not in result:
        result['type']='string' if all(isinstance(v,str) for v in result['enum']) else 'integer'
    if result.get('type')=='object':
        result['required']=list(result['properties'])
        result['additionalProperties']=False
    return result


def slot_results(phase, value, tasks):
    from hermes_source_units import ContractError
    expected={t['task'] for t in tasks}
    if not isinstance(value,dict) or set(value)!={'results'} or not isinstance(value['results'],dict):
        raise ContractError('INVALID_SCHEMA','$','return the fixed results object')
    slots=value['results'];items=[];failures=[]
    if set(slots)-expected:
        raise ContractError('INVALID_SCHEMA','$.results','unexpected task slots: '+','.join(sorted(set(slots)-expected)))
    for alias in sorted(expected):
        if alias not in slots:
            failures.append({'task':alias,'code':'INCOMPLETE_COVERAGE','message':'missing required task slot'})
            continue
        slot=slots[alias]
        if isinstance(slot,dict) and 'task' in slot:
            failures.append({'task':alias,'code':'INVALID_SCHEMA','message':'task identity is owned by its fixed slot'})
            continue
        if isinstance(slot,dict) and set(slot)=={'blocked_reason'}:
            failures.append({'task':alias,'code':'SEMANTIC_REVIEW_REQUIRED','message':slot['blocked_reason']})
            continue
        item={'task':alias,**slot} if isinstance(slot,dict) else slot
        try:
            validate(phase,{'drafts' if phase=='candidate' else 'reviews':[item]})
            items.append(item)
        except ContractError as exc:
            failures.append({'task':alias,'code':exc.code,'message':str(exc)})
    return {'drafts' if phase=='candidate' else 'reviews':items,'slot_failures':failures}
