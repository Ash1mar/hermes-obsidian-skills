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
