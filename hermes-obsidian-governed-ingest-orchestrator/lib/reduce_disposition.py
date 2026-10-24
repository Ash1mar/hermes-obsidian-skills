"""Explicit bounded Reduce dispositions; no domain answers or silent quota reset.

The stopped operator seals existing journals and current native planning. Workers
append at most one authorized revision, retaining every prior unknown/rejection.
Deferred evidence still blocks acceptance. Only exact duplicate observations may
be excluded without a coverage gap.
"""
import copy
import hashlib
from pathlib import Path

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint

SCOPE = 'reduce-disposition/v1'
CONTRACT = 'bounded-reduce-disposition/v1'
ACTIONS = {'revise-output', 'regenerate-output', 'revise-page-plan', 'defer', 'exclude-covered'}
PAGE_RULE = ('Write only the assigned candidates under this page identity. Other material in the original reading '
    'package is available for checking, not an additional obligation to express every table or topic. '
    'Prior evidence_qa is review narrative, not another factual claim. review/needs-qa labels and '
    'context_truncated or omitted-context counts alone do not prove required evidence is absent. '
    'Preserve actual limitations; never invent QA approval. Check each candidate against its own citations; '
    'identify concrete missing numbers, conditions, originals or required assets when refusing. '
    'Do not infer missing text, change citations, broaden scope, or approve unsupported claims.')


def fail(message):
    raise ContractError('RECOVERY_NOT_ALLOWED', '$', message)


def contract_hash():
    root = Path(__file__).resolve().parent
    return hashlib.sha256(b''.join((root / n).read_bytes() for n in (
        'reduce_disposition.py', 'semantic_output.py', 'fixed_page_work.py',
        'fixed_page_replay.py', 'reduce_execution_recovery.py', 'fixed_page_evidence.py', 'fixed_semantic.py'))).hexdigest()


def reports(adapter, binding):
    for pointer in adapter.workflow.status(binding['workflow_id']).get('execution_recovery_history', []):
        path = _vault_path(adapter.workflow.vault, pointer['report_ref'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != pointer['report_sha256']:
            fail('recovery report changed')
        report = _load_json(path)
        if report['request'].get('recovery_scope') == SCOPE:
            for entry in report['entries']:
                if entry['node'] == binding['node']:
                    yield pointer, entry


def output_permit(adapter, binding, path, record):
    found = []
    for pointer, entry in reports(adapter, binding):
        for row in entry.get('output_dispositions', []):
            if row['journal_name'] == path.name:
                n = row['preserved_attempt_count']
                if (row['input_sha256'] != record['input_sha256'] or row['schema_sha256'] != record['schema_sha256']
                        or fingerprint(record['attempts'][:n]) != row['prior_attempts_sha256']):
                    fail('sealed original attempts changed')
                found.append({**row, 'authorization_ref': pointer['report_ref']})
    if len(found) > 1:
        fail('one explicit disposition per logical output; another ID cannot reset budget')
    return found[0] if found else None


def planning_state(adapter, binding, value, permitted):
    """Reconstruct the accepted prefix, not a serialized assertion of state."""
    from fixed_global_replay import successful_prefix, restore_prefix
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_recovery import native_proof
    refs = successful_prefix(adapter, binding, value)
    prove = lambda p, w, s, i: native_proof(adapter, binding, p, w, s, i, permitted)
    state, remaining, pending, calls = restore_prefix(adapter, binding, value, refs, prepare_request_projection, prove)
    return state, remaining, pending, calls, prove


def verify(adapter, workflow, node, binding, entry, failure, runs):
    """Stopped admission authenticates current input, old executor and finite quota."""
    import fixed_reduce
    import fixed_global_packets as g
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_recovery import permitted_tasks
    from fixed_semantic_schema import schema_for
    from fixed_global_targets import planning_packet, select
    from semantic_output import path_for, CONTRACT as JOURNAL, check, read_saved_output
    from fixed_page_work import measure, refinement, accept_refinement
    from fixed_semantic import HermesCaller

    if node.name != 'global-reduce':
        fail('this disposition contract owns global planning and page outputs only')
    rows = entry.get('outputs')
    if not isinstance(rows, list) or not rows or len(rows) > 128:
        fail('select 1..128 exact outputs with an action and reason')
    if len({r.get('attempt_ref') for r in rows}) != len(rows):
        fail('duplicate selected journal')
    source = _vault_path(adapter.workflow.vault, entry['snapshot_ref'])
    value = _load_json(source)
    if (hashlib.sha256(source.read_bytes()).hexdigest() != entry['snapshot_sha256']
            or value != fixed_reduce.preview_snapshot(adapter, binding)):
        fail('current source, identity, evidence or snapshot changed')
    permitted = permitted_tasks(adapter, binding)
    permitted[binding['task_id']] = {'ended_run_ids': [str(r['id']) for r in runs]}
    state, remaining, pending, calls, prove = planning_state(adapter, binding, value, permitted)
    project = prepare_request_projection
    views = {}
    selected_phases={_load_json(_vault_path(adapter.workflow.vault,r['attempt_ref'])).get('phase') for r in rows}
    def offer(phase, view, packet=None, base=None):
        path = path_for(adapter, binding, phase, view)
        views[path.name] = (phase, view, packet, base)
    if remaining or pending:
        packet = (select(adapter, binding, 'global-detail', g.detail_packet(value, pending[0], state, project), project)
                  if pending else planning_packet(adapter, binding, value, remaining, state, project))
        offer('global-detail' if pending else 'global-plan', packet['view'], packet, state)
    # Authenticate existing budget refinements before offering their child pages.
    working = copy.deepcopy(state)
    page_caller=HermesCaller() if 'page-write' in selected_phases else None
    for page in list(state['pages']):
        if page_caller is None:break
        view, size = measure(adapter, value, page, page_caller, project)
        if size <= 30000:
            offer('page-write', view, page, None)
            continue
        base, packet = refinement(adapter, value, working, page, page_caller, project)
        packet = select(adapter, binding, 'global-detail', packet, project)
        jp = path_for(adapter, binding, 'global-detail', packet['view'])
        if not jp.exists():
            continue
        journal = _load_json(jp)
        last = journal['attempts'][-1]
        if last.get('state') != 'valid':
            offer('global-detail', packet['view'], packet, base)
            continue
        _, _, working = read_saved_output(adapter, binding, 'global-detail', packet['view'],
            lambda d: accept_refinement(adapter,value,base,packet,d,page_caller,project), prove, last['audit_ref'])
        for child in working['pages']:
            if set(child['candidate_refs']) <= set(page['candidate_refs']):
                cv, size = measure(adapter, value, child, page_caller, project)
                if size <= 30000:
                    offer('page-write', cv, child, None)
    sealed = []
    root = source.parent.parent / 'output-attempts'
    for row in rows:
        path = _vault_path(adapter.workflow.vault, row['attempt_ref'])
        action = row.get('action')
        if (path.parent != root or not path.is_file() or action not in ACTIONS
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()
                or hashlib.sha256(path.read_bytes()).hexdigest() != row.get('attempt_sha256')):
            fail('disposition journal, action, reason or digest differs')
        record = _load_json(path)
        if path.name not in views:
            fail('selected output is not a current authenticated planning/page target')
        phase, view, packet, base = views[path.name]
        sc = schema_for(phase, view)
        attempts = record.get('attempts', [])
        if (record.get('contract') != JOURNAL or record.get('phase') != phase or not attempts
                or record['input_sha256'] != fingerprint(view) or record['schema_sha256'] != fingerprint(sc)):
            fail('output input/schema differs')
        if output_permit(adapter, binding, path, record):
            fail('logical output already has a disposition; use its remaining authority')
        for i, attempt in enumerate(attempts, 1):
            prior = permitted.get(attempt.get('task_id'))
            if (attempt.get('attempt') != i or attempt.get('attempt_id') != fingerprint({'path': path.name, 'attempt': i})
                    or not prior or str(attempt.get('run_id')) not in prior['ended_run_ids']):
                fail('old journal executor is not authenticated and stopped')
            from fixed_semantic_media import image_catalog, select_images
            images = select_images(view['original_evidence']['tasks'], image_catalog(adapter, view['original_evidence'])) if 'original_evidence' in view else []
            prepared = project(phase, view, sc, images, attempt.get('correction'))
            if attempt.get('wire_sha256') != fingerprint(prepared['wire']):
                fail('old full request changed')
            if attempt.get('audit_ref'):
                ap = _vault_path(adapter.workflow.vault, attempt['audit_ref'])
                found = prove(ap, prepared['wire'], sc, images)
                if found is None:
                    fail('old native response cannot be authenticated')
        last = attempts[-1]
        if last.get('state') == 'valid':
            fail('reuse valid output; no revision grant for accepted results')
        if action == 'regenerate-output' and last.get('audit_ref'):
            fail('regeneration is for unknown completion; use explicit revision for a rejected result')
        if action == 'revise-output' and not last.get('audit_ref'):
            fail('unknown completion requires explicit regeneration authority')
        if action == 'revise-page-plan' and phase != 'page-write':
            fail('local page planning revision requires a held page')
        clarification = {'contract': CONTRACT, 'action': action,
            'instruction': PAGE_RULE if phase == 'page-write' else
                'Return a complete legal replacement for this exact packet. Use only the offered t references; '
                'never infer aliases between h and t. Preserve each proposal exactly once and all scope distinctions.',
            'previous_failure': {k:v for k,v in last.get('diagnostic', {}).items()
                if k in ('code','reason','rule','page_findings','missing','unknown')},
            'old_unknown_execution_preserved': not bool(last.get('audit_ref'))}
        if phase != 'page-write':
            from fixed_global_targets import offered
            clarification['allowed_target_refs'] = [t.get('target_ref') for t in offered(view) if t.get('target_ref')]
        projected = project(phase, view, sc, [], clarification)
        if action in ('revise-output', 'regenerate-output') and projected['input_size'] > 30000:
            fail('complete revision exceeds request bound; no truncation or budget grant')
        sealed.append({'journal_name': path.name, 'phase': phase, 'action': action,
            'reason': row['reason'], 'input_sha256': record['input_sha256'], 'schema_sha256': record['schema_sha256'],
            'preserved_attempt_count': len(attempts), 'prior_attempts_sha256': fingerprint(attempts),
            'attempt_ordinal': len(attempts) + 1, 'additional_calls': int(action in ('revise-output', 'regenerate-output','revise-page-plan')),
            'correction': clarification, 'page': packet if phase == 'page-write' else None,
            'coverage_gap': action == 'defer'})
        if action == 'revise-page-plan':
            sealed[-1].update(planning_calls=1, child_body_calls=4,
                rule='one native local plan; at most four children, each one body call; preserve every candidate')
        if action == 'exclude-covered':
            # Existing global snapshots already deduplicate identical observations.
            # A new exact proof must name a different current covering candidate;
            # similarity, keywords or an operator-authored summary are insufficient.
            target = row.get('covered_by_candidate')
            selected = {c['candidate']: c for c in value['view']['candidates']}
            handles = view.get('candidates', [])
            if phase != 'page-write' or len(handles) != 1 or target not in selected:
                fail('no-loss exclusion requires one exact duplicate candidate and a covering target')
            original = handles[0]
            canonical = lambda c: {k: v for k, v in c.items() if k != 'candidate'}
            if target == original['candidate'] or canonical(original) != canonical(selected[target]):
                fail('no proven exact duplicate; retain coverage and use native revision or defer')
            sealed[-1]['covered_by_candidate'] = target
    return {'output_dispositions': sealed, 'authenticated_prefix_count': len(calls),
        'source_snapshot_sha256': entry['snapshot_sha256'], 'original_calls_preserved': True,
        'automatic_corrections_granted': 0, 'accepted_coverage_gaps': False}


def read_recovery(adapter,binding,phase,view,validator,prove,expected_ref,path,record,permit):
    """Authenticate the single appended result and its sealed historical prefix."""
    from semantic_output import held
    from fixed_semantic import prepare_request_projection, decode_semantic_response
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog, select_images
    n=permit['preserved_attempt_count'];attempts=record['attempts']
    if len(attempts)!=n+1:held('explicit recovery has no unique final output')
    a=attempts[n];sc=schema_for(phase,view)
    images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence'])) if 'original_evidence' in view else []
    prepared=prepare_request_projection(phase,view,sc,images,permit['correction'])
    if (a.get('attempt')!=n+1 or a.get('attempt_id')!=fingerprint({'path':path.name,'attempt':n+1})
            or a.get('authorization_ref')!=permit['authorization_ref'] or a.get('correction')!=permit['correction']
            or a.get('wire_sha256')!=fingerprint(prepared['wire']) or a.get('audit_ref')!=expected_ref
            or not expected_ref or prepared['input_size']>30000):
        held('explicit recovery request, ordinal or authorization changed')
    ap=_vault_path(adapter.workflow.vault,expected_ref);audit=_load_json(ap)
    if (audit.get('native_task_id')!=a.get('task_id') or str(audit.get('native_run_id'))!=str(a.get('run_id'))
            or audit.get('output_attempt_id')!=a['attempt_id']
            or a.get('session_id') and audit.get('model',{}).get('session_id')!=a['session_id']):
        held('explicit recovery audit differs from its journal')
    proof=prove(ap,prepared['wire'],sc,images)
    if proof is None:held('explicit recovery lacks native transcript proof')
    try:draft,_=decode_semantic_response(proof[0])
    except __import__('json').JSONDecodeError as exc:
        raise ContractError('INVALID_SCHEMA','$','authorized recovery response is not JSON') from exc
    result=validator(draft)
    if a.get('state')!='valid':held('recovery output has not been durably validated')
    return draft,expected_ref,result


def page_permit(adapter,binding,page):
    found=[]
    for pointer,entry in reports(adapter,binding):
        for row in entry.get('output_dispositions',[]):
            p=row.get('page')
            if p and p['path']==page['path'] and p['candidate_refs']==page['candidate_refs'] and p['identity']==page['identity']:
                found.append({**row,'authorization_ref':pointer['report_ref']})
    if len(found)>1:fail('duplicate page disposition')
    return found[0] if found else None


def revision_packet(value,state,page,permit):
    """Native local regrouping, without changing citation claims or omissions."""
    import fixed_global_packets as g
    import fixed_reduce
    handles=set(page['candidate_refs'])
    sliced={'view':{'phase':'global-detail',
        'candidates':[c for c in value['view']['candidates'] if c['candidate'] in handles],
        'proposals':[],'existing_pages':[p for p in value['view'].get('existing_pages',[])
            if p['identity']['kind']==page['identity']['kind'] and p['identity']['identity_key']==page['identity']['identity_key']]}}
    view=fixed_reduce.global_detail_view(sliced)
    base=copy.deepcopy(state);base['pages']=[p for p in base['pages'] if p!=page]
    view['coordination']={'contract':g.CONTRACT,'previous_state_sha256':g.state_digest(base),'proposal_refs':[],
        'planned_pages':[{'identity':copy.deepcopy(page['identity']),'path':page['path'],'topics':[],'source_paths':[]}],
        'rule':'Explicit bounded page scope revision: preserve EVERY assigned candidate exactly once; no omissions. '
            'Choose compatible identities and paths; retain the original identity for a suitable subset when justified. '
            'Return at most four pages. Other occupied identities/paths cannot be appended without being offered. '
            'Do not change candidate claims, citations or source scope. Prior refusal is diagnostic, not evidence.'}
    view['scope_revision']={'contract':CONTRACT,'parent_journal':permit['journal_name'],
        'authorization_ref':permit['authorization_ref'],'reason':permit['reason'],
        'prior_failure':permit['correction']['previous_failure'],'max_pages':4}
    return base,{'view':view,'previous_state_sha256':g.state_digest(base),'candidate_refs':page['candidate_refs'],
        'proposal_refs':[],'units':[],'unit_count':1}


def execute_output(adapter, binding, phase, view, caller, model_call, validator, path, record, permit):
    """Exactly one appended native attempt, no retry of any old unknown call."""
    from semantic_output import ACTIVE, write, held, note_audit
    from fixed_semantic import prepare_request_projection, decode_semantic_response, attach_receipt
    from fixed_semantic_schema import schema_for
    from fixed_semantic_recovery import native_reply
    from fixed_semantic_media import image_catalog, select_images
    if permit['action'] not in ('revise-output', 'regenerate-output','revise-page-plan'):
        held('explicit ' + permit['action'] + ' disposition holds this body until planning/coverage is resolved')
    n = permit['preserved_attempt_count']
    correction = permit['correction']
    sc = schema_for(phase, view)
    images = select_images(view['original_evidence']['tasks'], image_catalog(adapter, view['original_evidence'])) if 'original_evidence' in view else []
    prepared = prepare_request_projection(phase, view, sc, images, correction)
    if prepared['input_size'] > 30000:
        held('authorized complete recovery request exceeds bound')
    if len(record['attempts']) > n + 1:
        held('explicit recovery budget exceeded')
    if len(record['attempts']) == n + 1:
        attempt = record['attempts'][n]
        if (attempt.get('wire_sha256') != fingerprint(prepared['wire']) or attempt.get('correction') != correction
                or attempt.get('authorization_ref') != permit['authorization_ref']
                or attempt.get('attempt_id') != fingerprint({'path': path.name, 'attempt': n + 1})):
            held('recovery attempt identity changed')
        if not attempt.get('audit_ref'):
            held('authorized regeneration completion is unknown; no further call')
        proof = native_reply(adapter, binding, _vault_path(adapter.workflow.vault, attempt['audit_ref']), prepared['wire'], sc, images)
        if proof is None:
            held('recovery response lacks genuine native proof')
        if attempt.get('state')!='valid':held('explicit recovery exhausted or uncertain; no automatic correction')
        return read_recovery(adapter,binding,phase,view,validator,
            lambda *args:proof,attempt['audit_ref'],path,record,permit)
    import os
    adapter.worker_check(binding)
    attempt = {'attempt': n + 1, 'attempt_id': fingerprint({'path': path.name, 'attempt': n + 1}),
        'task_id': binding['task_id'], 'run_id': os.environ.get('HERMES_KANBAN_RUN_ID'),
        'wire_sha256': fingerprint(prepared['wire']), 'correction': copy.deepcopy(correction),
        'authorization_ref': permit['authorization_ref'], 'state': 'reserved'}
    record['attempts'].append(attempt)
    write(path, record)
    token = ACTIVE.set((path, record, n))
    try:
        draft, ref = model_call(adapter, binding, phase, view, caller, 30000, correction, attempt_limit=2)
        note_audit(ref)
        result = validator(draft)
        attempt.update(state='valid', correctable=False)
        write(path, record)
        return draft, ref, result
    except ContractError as exc:
        ref = attempt.get('audit_ref') or getattr(exc, 'semantic_audit_ref', None)
        if ref:
            attempt.update(state='rejected', audit_ref=ref, correctable=False,
                diagnostic={'code': exc.code, 'reason': str(exc)})
            attach_receipt(adapter, ref, {'ok': False, 'validated': False, 'state': 'explicit_recovery_held',
                'code': exc.code, 'reason': str(exc)})
            exc.semantic_audit_ref = ref
            write(path, record)
        raise
    finally:
        ACTIVE.reset(token)
