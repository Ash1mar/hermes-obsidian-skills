"""Immutable future-work presentation pin, fixed before first dispatch."""
from .source_units import _load_json,_write_atomic,_json_bytes,_exclusive_lock
from .validation import ContractError,fingerprint

CONTRACT='efficiency-presentation/v1'

def pin(workflow,w):
    root=workflow.vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}"
    path=root/'presentation-contract.json'
    identity={'workflow_id':w['workflow_id'],'scope_sha256':fingerprint(w['scope'])}
    if w.get('workflow_test') and not w['workflow_test']['presentation_reused']:
        from .workflow_test import check_admission
        check_admission(workflow,w)
        identity['workflow_test_operation']=w['workflow_test']['operation_id']
    if w.get('reduce_test'):
        pointer=w['reduce_test'];report_path=workflow.vault/pointer['report_ref']
        import hashlib,json
        raw=report_path.read_bytes();report=json.loads(raw)
        if (hashlib.sha256(raw).hexdigest()!=pointer['report_sha256']
                or report.get('contract')!='same-workflow-reduce-test/v1'
                or report['request']['workflow_id']!=w['workflow_id']
                or report['request']['operation_id']!=pointer['operation_id']
                or report.get('new_pass_calls_allowed')!=0):
            raise ContractError('STALE_INPUT','$','Reduce test admission changed')
        identity['reduce_test_operation']=pointer['operation_id']
    with _exclusive_lock(path.with_suffix('.lock'),timeout=5):
        if path.exists():record=_load_json(path)
        else:
            consumed=any((root/'semantic-calls').glob('*.json')) or any((root/'output-attempts').glob('*.json'))
            if w.get('reduce_test'):
                # The explicit test admission retires downstream work, retaining
                # accepted Pass calls and their cumulative budget in place.
                consumed=any(_load_json(p).get('phase') not in ('candidate','citation')
                    for p in (root/'semantic-calls').glob('*.json')) or any((root/'output-attempts').glob('*.json'))
            if w.get('workflow_test') and not w['workflow_test']['presentation_reused']:
                consumed=any(_load_json(p).get('phase') in ('page-write','page-pair','page-review')
                    for name in ('semantic-calls','output-attempts') for p in (root/name).glob('*.json'))
            budget=root/'pass-call-budget.json'
            if budget.exists() and not w.get('reduce_test') and not w.get('workflow_test'):consumed=consumed or any(r.get('attempts') for r in _load_json(budget).get('tasks',{}).values())
            record={**identity,'contract':'legacy' if consumed else CONTRACT}
            if not consumed:
                from .input_budget import profile,CONTRACT as BUDGET
                record.update(input_budget_contract=BUDGET,input_budget=profile(w['scope'].get('execution_policy',{}).get('input_budget_profile')))
                record['review_call_contract']='independent-page-review/v1'
                record['review_risk_contract']='explicit-page-risks/v2'
                record.update(page_call_contract='durable-page-calls/v1',page_dispatch_contract='per-page-dispatch/v2',
                    planning_budget_contract='shared-reading-page-budget/v1',fixed_before_consumption=True,
                    page_execution_contract='unified-page-execution/v3',page_budget_contract='page-budget-index/v2')
            _write_atomic(path,_json_bytes(record))
    if any(record.get(k)!=v for k,v in identity.items()) or record.get('contract') not in ('legacy',CONTRACT):
        raise ContractError('STALE_INPUT','$','presentation identity changed')
    if w.get('page_execution_contract') and record.get('page_execution_contract')!=w['page_execution_contract']:
        raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','workflow and presentation protocol differ')
    if record.get('input_budget_contract'):
        from .input_budget import validate
        validate(record.get('input_budget'))
    return record
