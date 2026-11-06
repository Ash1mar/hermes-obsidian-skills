"""Immutable future-work presentation pin, fixed before first dispatch."""
from .source_units import _load_json,_write_atomic,_json_bytes,_exclusive_lock
from .validation import ContractError,fingerprint

CONTRACT='efficiency-presentation/v1'

def pin(workflow,w):
    root=workflow.vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}"
    path=root/'presentation-contract.json'
    identity={'workflow_id':w['workflow_id'],'scope_sha256':fingerprint(w['scope'])}
    with _exclusive_lock(path.with_suffix('.lock'),timeout=5):
        if path.exists():record=_load_json(path)
        else:
            consumed=any((root/'semantic-calls').glob('*.json')) or any((root/'output-attempts').glob('*.json'))
            budget=root/'pass-call-budget.json'
            if budget.exists():consumed=consumed or any(r.get('attempts') for r in _load_json(budget).get('tasks',{}).values())
            record={**identity,'contract':'legacy' if consumed else CONTRACT}
            if not consumed:
                record.update(page_call_contract='durable-page-calls/v1',page_dispatch_contract='per-page-dispatch/v2',
                    planning_budget_contract='shared-reading-page-budget/v1',fixed_before_consumption=True,
                    page_execution_contract='unified-page-execution/v3',page_budget_contract='page-budget-index/v2')
            _write_atomic(path,_json_bytes(record))
    if any(record.get(k)!=v for k,v in identity.items()) or record.get('contract') not in ('legacy',CONTRACT):
        raise ContractError('STALE_INPUT','$','presentation identity changed')
    if w.get('page_execution_contract') and record.get('page_execution_contract')!=w['page_execution_contract']:
        raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','workflow and presentation protocol differ')
    return record
