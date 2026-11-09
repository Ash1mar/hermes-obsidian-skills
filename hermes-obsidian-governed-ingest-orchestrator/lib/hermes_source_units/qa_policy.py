"""Shared no-human QA policy. Labels are metadata, never evidence approval."""
import copy
from .validation import ContractError,fingerprint
from .source_units import _vault_path,_write_atomic,_json_bytes,_load_json

POLICY={'contract':'automatic-qa-policy/v1','labels':'advisory',
        'blocking_action':'isolate_with_dependents','max_additional_hermes_checks':1,
        'human_review_required':False,'invalid_evidence_accepted':False}
STAGES=('bootstrap','source_preparing','planning','canary','analyzing','resource_reduce',
        'global_planning','global_writing','checkpoint_1','build_finalizing',
        'release_planning','checkpoint_2','applying','indexing','validating')
SEMANTIC_PHASES={'candidate','citation','resource-reduce','global-plan','global-detail','page-write','page-review'}


def enabled(workflow):
    # Staged execution retains its existing explicit control boundaries.
    return workflow.get('scope',{}).get('execution_mode')=='auto_full'


def disposition(*,blocking=False,known_response=True,additional_checks=0):
    if not blocking:return 'continue_with_qa_metadata'
    if not known_response:return 'hold_execution_uncertainty'
    if type(additional_checks) is not int or additional_checks<0:raise ContractError('INVALID_SCHEMA','$','invalid QA call count')
    return 'isolate_with_dependents'


def gap(vault,workflow_id,node,scope,evidence,*,dependents=()):
    value={'contract':'automatic-qa-gap/v1','workflow_id':workflow_id,'node':node,
        'policy':copy.deepcopy(POLICY),'scope':copy.deepcopy(scope),'evidence':copy.deepcopy(evidence),
        'dependent_exclusions':copy.deepcopy(list(dependents)),'coverage_complete':False,
        'domain_approval_inferred':False,'human_review_requested':False,
        'cause':'unconfirmed_source_or_model_quality','reopen_requires':'new usable evidence or a supported verified program correction'}
    ref=f'_system/ledgers/ingest-workflows/{workflow_id}/qa-gaps/{fingerprint(value)}.json'
    _write_atomic(_vault_path(vault,ref),_json_bytes(value))
    return ref


def annotation_disposition(issue, materials):
    """Preserve labels and distinguish a documented defect in used material."""
    affected = sorted(set(materials).intersection(issue['items']))
    hard = bool(affected and issue['status'] == 'localized'
                and (issue['writing'] == 'hold' or issue['publication'] == 'hold'))
    return {'original': issue, 'matched_materials': affected,
            'effect': 'material-hold' if hard else 'annotation'}


def reports(vault,workflow_id):
    root=_vault_path(vault,f'_system/ledgers/ingest-workflows/{workflow_id}/qa-gaps')
    refs=[]
    for path in sorted(root.glob('*.json')):
        value=_load_json(path)
        if (value.get('contract')!='automatic-qa-gap/v1' or value.get('workflow_id')!=workflow_id
                or fingerprint(value)!=path.stem or value.get('coverage_complete') is not False):
            raise ContractError('STALE_INPUT','$','QA gap evidence changed')
        refs.append(path.relative_to(vault).as_posix())
    return refs
