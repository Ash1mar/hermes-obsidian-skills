"""Future-only execution contract; historical request bytes remain reconstructible."""
from hermes_source_units import ContractError
CONTRACT='unified-page-execution/v3'
BUDGET='page-budget-index/v2'
def strict(view):
    declared=view.get('page_execution_contract')
    if declared is not None and declared!=CONTRACT:
        raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','unknown pinned execution version; no fallback')
    return declared==CONTRACT
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
            or not all(callable(f) for f in (execute,read_outcome,create,build_plan,prepare_request_projection,
                getattr(PageCalls,'reserve',None),getattr(PageCalls,'capture',None),getattr(PageCalls,'settle',None)))):
        raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','request/budget/receipt/recovery/publication support is incomplete; no consumption permitted')
    schema_for('page-pair',{'pages':{'p1':{}},'page_execution_contract':CONTRACT})
    schema_for('page-pair',{'pages':{'p1':{},'p2':{}},'page_execution_contract':CONTRACT})
    if profile.get('body_call_contract'):
        from independent_body import CONTRACT as BODY,prepare,restore,present
        if profile['body_call_contract']!=BODY or not all(callable(f) for f in (prepare,restore,present)):
            raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','independent body construction and recovery are required')
    if profile.get('page_semantics_policy'):
        from hermes_source_units.generation_policy import POLICY,validate_run,capture,assert_model_phase
        if profile['page_semantics_policy']!=POLICY or not all(callable(f) for f in (validate_run,capture,assert_model_phase)):
            raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','generation-only provenance, Build and model-dispatch fencing must be installed')
    if profile.get('body_pairing_contract'):
        from page_single_admission import CONTRACT as PAIRING,admit,matches
        if profile['body_pairing_contract']!=PAIRING or not all(callable(f) for f in (admit,matches)):
            raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','single admission reuse must be installed before pairing')
    if profile.get('review_call_contract'):
        from paired_reviews import execute as review_execute,history
        from hermes_source_units.review_receipts import validate_receipt,CONTRACT as REVIEW
        if (profile['review_call_contract']!=REVIEW or not profile.get('input_budget')
                or not all(callable(f) for f in (review_execute,history,validate_receipt))):
            raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','review request, budget, recovery and publication must support the same contract')
        schema_for('page-review-pair',{'pages':{'p1':{},'p2':{}}})
