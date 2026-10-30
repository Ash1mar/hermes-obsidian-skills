"""Evidence-bound Build inventory. Opinions never become automatic approvals.

This is a preparation/deferral contract, not a publication or retry authority.
Its logical identities exclude dispatch/run operation IDs. Original finite
journals, native responses, Passes and page revisions remain untouched.
"""
import hashlib
from collections import Counter

from hermes_source_units.source_units import _load_json, _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint

CONTRACT = 'build-evidence-disposition/v1'
REOPEN = [
    'verified_general_program_fix_with_affected_input_diff',
    'materially_improved_hash_valid_evidence',
    'authenticated_existing_native_completion',
    'explicit_supported_disposition_with_new_evidence',
]


def _pin(vault, ref):
    raw = _vault_path(vault, ref).read_bytes()
    return {'ref': ref, 'sha256': hashlib.sha256(raw).hexdigest()}


def journal_evidence(adapter, binding, view):
    """Capture exact finite history; never infer no-call from missing evidence."""
    from semantic_output import path_for
    vault = adapter.workflow.vault
    path = path_for(adapter, binding, 'page-review', view)
    if not path.exists():
        return {'attempt_count': 0, 'attempts': [], 'journal': None}
    ref = path.relative_to(vault).as_posix()
    journal = _load_json(path)
    attempts = []
    for attempt in journal.get('attempts', []):
        item = {k: attempt[k] for k in ('attempt_id', 'state', 'session_id', 'wire_sha256', 'diagnostic') if k in attempt}
        item['attempt_sha256'] = fingerprint(attempt)
        item['audit'] = _pin(vault, attempt['audit_ref']) if attempt.get('audit_ref') else None
        pointer = attempt.get('call_outcome')
        if pointer:
            report_ref = (path.parent.parent / pointer['report_ref']).relative_to(vault).as_posix()
            item['outcome'] = _pin(vault, report_ref)
            item['completion_state'] = _load_json(_vault_path(vault, report_ref)).get('state')
        elif not item['audit']:
            item['completion_state'] = 'unproven_stopped_attempt' if (
                attempt.get('diagnostic', {}).get('code') == 'WORKFLOW_STOPPED'
            ) else 'completion_unknown'
        attempts.append(item)
    return {'journal': _pin(vault, ref), 'input_sha256': journal.get('input_sha256'),
            'schema_sha256': journal.get('schema_sha256'), 'attempt_count': len(attempts), 'attempts': attempts}


def item(adapter, binding, value, view, *, state, result=None, error=None, audit_ref=None):
    """Record observed status separately from unconfirmed causal attribution."""
    vault = adapter.workflow.vault
    page = value['backend']['pages'][view['page']]
    revision = page['page']
    identity = {k: revision.get(k) for k in ('page_id', 'revision_id', 'authored_sha256')}
    logical = {'run_id': page['run_id'], **identity}
    history = journal_evidence(adapter, binding, view)
    statuses = {a.get('completion_state') for a in history['attempts']}
    if 'unproven_stopped_attempt' in statuses:
        observed = 'unproven_stopped_attempt'
    elif 'completion_unknown' in statuses:
        observed = 'completion_unknown'
    elif result:
        observed = result['decision']
    elif error and error.code == 'SEMANTIC_REVIEW_REQUIRED':
        observed = 'explicit_refusal'
    else:
        observed = 'execution_held' if error else 'program_accepted'
    # A valid later response does not erase a historical uncertain ordinal.
    # Status of the current authenticated result and historical uncertainty are
    # both retained; this contract grants no replay of either one.
    if result:
        observed = result['decision']
    from hermes_source_units.finalize_revision_state import load,work_id
    control=load(vault,binding['workflow_id'])['works'].get(work_id(page['run_id'],revision['page_id']))
    if not result and control and control.get('grants'):
        uncertain=[c for g in control['grants'] for c in g['calls'].values() if c['state'] in ('reserved','held') and not c.get('audit')]
        if uncertain:observed='completion_unknown'
    decision = next((d for run in value['backend']['runs'] if run['run_id'] == page['run_id']
                     for d in run.get('decisions', []) if d['page_id'] == revision['page_id']), {})
    evidence = {'candidate_refs': decision.get('candidate_refs', []),
                'original_packet_sha256': fingerprint(view.get('original_evidence', {})),
                'citation_evidence_sha256': fingerprint(view.get('citation_evidence', [])),
                'assessment': page.get('assessment'), 'parent_authored_sha256': page.get('parent_hash')}
    return {'issue_id': fingerprint(logical), 'logical_work': logical, 'page': view['page'],
            'path': view.get('path'), 'state': state, 'observed_status': observed,
            'current_input_sha256': fingerprint(view), 'evidence': evidence, 'history': history,
            'native_audit': _pin(vault, audit_ref) if audit_ref else None,
            'cumulative_calls':{'baseline':control.get('baseline_calls',{}) if control else {},
                'additional_consumed':{phase:sum(phase in g['calls'] for g in control.get('grants',[])) for phase in ('page-write','page-review')} if control else {},
                'controlled_grants':control.get('grants',[]) if control else []},
            'original_decision': result, 'diagnostic': {'code': error.code, 'reason': str(error)} if error else None,
            'attribution': {'status': 'pending_evidence_comparison' if state == 'deferred' else 'not_required',
                'confirmed_cause': None,
                'candidates': ['source_conversion', 'program_assembly_or_mapping', 'model_generation', 'model_review']
                    if observed in ('revision_required', 'explicit_refusal') else
                    ['execution_uncertainty'] if state == 'deferred' else [],
                'rule': 'Find the earliest verified divergence between originals, registered assets, actual wire, candidate, page and review. A review opinion is not a confirmed content error.'},
            'retry_policy': {'unchanged_input_new_calls': 0, 'operation_id_resets_allowance': False,
                'reopen_requires': REOPEN, 'new_calls_authorized': False},
            'effect': {'ready_for_submission': state == 'ready', 'coverage_completed': False,
                       'publication_authorized': False, 'affected_dependents_held': state == 'deferred'}}


def persist(adapter, binding, value, snapshot_ref, rows, reviews, acceptances):
    """Durable ready/deferred partition; no mutation of original domain results."""
    vault = adapter.workflow.vault
    pages = value['backend']['pages']
    if {r['page'] for r in rows} != set(pages) or len(rows) != len(pages):
        from hermes_source_units import ContractError
        raise ContractError('INCOMPLETE_COVERAGE', '$', 'Build disposition must account for every original page exactly once')
    ready = {r['page'] for r in rows if r['state'] == 'ready'}
    if ready != set(reviews) or not set(acceptances).issubset(ready):
        from hermes_source_units import ContractError
        raise ContractError('REVIEW_REQUIRED', '$', 'ready partition differs from authenticated review results')
    prepared = []
    for alias in sorted(ready):
        page = pages[alias]
        prepared.append({'run_id': page['run_id'], 'page_id': page['page']['page_id'],
            'authored_sha256': page['page']['authored_sha256'], 'parent_authored_sha256': page['parent_hash'],
            'actor': value['backend']['actor'], 'note': reviews[alias],
            **({'acceptance': acceptances[alias]} if alias in acceptances else {})})
    from build_evidence import check
    evidence_checks=check(value)
    if not evidence_checks['ok']:
        from hermes_source_units import ContractError
        raise ContractError('INCOMPLETE_COVERAGE','$','Build reference/material mapping is incomplete; retain affected work')
    report = {'contract': CONTRACT, 'binding': dict(binding), 'input_id': value['input_id'],
        'evidence_checks':evidence_checks,
        'snapshot': _pin(vault, snapshot_ref), 'items': rows, 'prepared_reviews': prepared,
        'counts': dict(Counter(r['observed_status'] for r in rows)), 'ready_count': len(ready),
        'deferred_count': len(rows)-len(ready), 'unchanged_input_new_calls': 0,
        'finalized': False, 'coverage_complete': False, 'publication_authorized': False,
        'submission_rule': 'Ready pages may commit through partial-build-finalize/v1. Deferred/unknown pages and source completion remain held; partial commits never release publication or stage acceptance.'}
    # Content addressing preserves previous inventories even after new evidence.
    ref = f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/build-dispositions/{fingerprint(report)}.json"
    _write_atomic(_vault_path(vault, ref), _json_bytes(report))
    return ref
