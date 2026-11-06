"""Test-only single semantic generation policy, pinned before any consumption."""
import hashlib
from pathlib import Path
from .source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from .validation import ContractError,fingerprint
POLICY='generation-only-page-semantics/v1'

def fail(message):raise ContractError('PAGE_REVIEW_POLICY_CHANGED','$',message)

def test_scope(vault):
    identity=Path(vault)/'_system/vault.json';retrieval=Path(vault)/'_system/metadata/retrieval-policy.json'
    if not identity.exists() or not retrieval.exists():return False
    return ('test' in _load_json(identity)['vault']['security_domain'].lower()
        and _load_json(retrieval).get('query_enabled') is False)

def select(vault,w):
    choice=w['scope'].get('execution_policy',{}).get('independent_page_review')
    test=test_scope(vault)
    if choice=='disabled' and not test:fail('generation-only default is limited to Query-disabled test Vaults')
    return POLICY if test and choice!='enabled' else None

def pinned(vault,wid):
    path=Path(vault)/f'_system/ledgers/ingest-workflows/{wid}/presentation-contract.json'
    return _load_json(path).get('page_semantics_policy') if path.exists() else None

def capture(vault,w,actor):
    """Domain writer calls this under its ordinary workflow transaction lock."""
    path=Path(vault)/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/presentation-contract.json';p=_load_json(path)
    if p.get('page_semantics_policy')!=POLICY:return None
    if p.get('scope_sha256')!=fingerprint(w['scope']) or p.get('workflow_id')!=w['workflow_id'] or actor!=w['actor'] or select(vault,w)!=POLICY:
        fail('new generation-only run lacks its fixed test identity and policy')
    record={'contract':POLICY,'workflow_id':w['workflow_id'],'actor':actor,'presentation':p,
        'independent_review':'not_performed','semantic_responsibility':'page_generation_model'}
    ref=f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/semantic-policies/{fingerprint(record)}.json';raw=_json_bytes(record);target=_vault_path(vault,ref)
    if target.exists() and target.read_bytes()!=raw:fail('immutable semantic policy changed')
    if not target.exists():_write_atomic(target,raw)
    return {'contract':POLICY,'workflow_id':w['workflow_id'],'ref':ref,'sha256':hashlib.sha256(raw).hexdigest()}

def new_run(service,actor):
    from .workflow_guard import _ACTIVE
    active=_ACTIVE.get()
    if not active or active['kind']!='global-reduce':return None
    w=_load_json(_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{active["workflow_id"]}.json'))
    return capture(service.vault,w,actor)

def validate_run(service,run):
    pointer=run.get('page_review_policy')
    if not pointer or pointer.get('contract')!=POLICY:fail('Build run has no generation-only policy fixed at creation')
    raw=_vault_path(service.vault,pointer['ref']).read_bytes();p=_load_json(_vault_path(service.vault,pointer['ref']))
    expected=f'_system/ledgers/ingest-workflows/{pointer["workflow_id"]}/semantic-policies/{fingerprint(p)}.json'
    if (hashlib.sha256(raw).hexdigest()!=pointer['sha256'] or pointer['ref']!=expected
            or p.get('contract')!=POLICY or p.get('workflow_id')!=pointer['workflow_id'] or p.get('actor')!=run['actor']
            or p.get('presentation',{}).get('page_semantics_policy')!=POLICY
            or p.get('independent_review')!='not_performed'):
        fail('Build semantic policy provenance changed')
    return p

def assert_model_phase(vault,binding,phase):
    if phase in ('page-review','page-review-pair') and pinned(vault,binding['workflow_id'])==POLICY:
        raise ContractError('INDEPENDENT_PAGE_REVIEW_DISABLED','$','pinned generation-only test policy forbids independent model review')
