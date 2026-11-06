"""Future-only execution contract; historical request bytes remain reconstructible."""
from hermes_source_units import ContractError
CONTRACT='unified-page-execution/v3'
BUDGET='page-budget-index/v2'
def strict(view):return view.get('page_execution_contract')==CONTRACT
def page_rules():
    from reduce_disposition import PAGE_RULE
    return PAGE_RULE+' Each result slot owns its exact page identity, candidates, applicability and necessary evidence. Shared context is retained for checking, never an instruction to merge scope. QA notes and truncation notices remain evidence; determine their actual effect on assigned claims. Missing required support must be reported, never inferred.'
def preflight(profile):
    if not strict(profile):return
    from page_pair_calls import PageCalls
    from native_page_pairs import execute
    from durable_page_replay import read_outcome
    import durable_page_replay
    from hermes_source_units.page_governance import create
    from hermes_source_units.scoped_release import build_plan
    from fixed_semantic_schema import schema_for
    from fixed_semantic import prepare_request_projection
    if (profile.get('page_budget_contract')!=BUDGET or getattr(PageCalls,'storage_contract',None)!=BUDGET
            or getattr(durable_page_replay,'SUPPORTED_EXECUTION_PROTOCOL',None)!=CONTRACT
            or not all(callable(f) for f in (execute,read_outcome,create,build_plan,prepare_request_projection))):
        raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','request/budget/receipt/recovery/publication support is incomplete; no consumption permitted')
    schema_for('page-pair',{'pages':{'p1':{}},'page_execution_contract':CONTRACT})
