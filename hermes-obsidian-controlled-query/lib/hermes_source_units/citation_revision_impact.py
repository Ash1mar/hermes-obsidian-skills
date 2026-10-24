"""Read-only dependency proofs for a consumed citation amendment.

This module never reopens a Pass, retires a result, grants calls or approves
reuse. In particular, disjoint claim membership is weaker than unchanged
worker input: a successor changes the Pass identity and may change shared QA.
"""
import copy
import hashlib

from .source_units import _load_json, _vault_path
from .validation import ContractError, fingerprint, validate_record

CONTRACT = 'citation-revision-impact/v1'


def fail(message):
    raise ContractError('REVISION_IMPACT_INVALID', '$', message)


def candidate_key(ref):
    if not isinstance(ref, dict) or set(ref) != {'pass_id', 'candidate_id'}:
        fail('candidate dependency needs its canonical Pass and candidate identity')
    return ref['pass_id'], ref['candidate_id']


def successor_delta(prior, successor, selected_ids):
    """Verify a native successor's bounded edits; not native provenance proof.

    New/deleted candidates require a separately reviewed planning change. This
    narrow comparison preserves every unselected candidate byte for byte.
    """
    for record in (prior, successor):
        validate_record('knowledge_pass', record)
        if fingerprint({k: v for k, v in record.items() if k != 'pass_id'}) != record['pass_id']:
            fail('citation fingerprint differs')
    if (prior['pass_kind'] != 'citation' or successor['pass_kind'] != 'citation'
            or successor.get('supersedes_pass_id') != prior['pass_id']
            or not successor.get('revision_id') or successor['sequence'] != prior['sequence'] + 1
            or any(prior[k] != successor[k] for k in (
                'task_id', 'actor', 'reading_window_id', 'reading_package_id'))
            or any(prior.get(k) != successor.get(k) for k in ('batch_id', 'reading_package_fingerprint'))):
        fail('successor must retain the original task, source window and immediate predecessor')
    old = {c['candidate_id']: c for c in prior['candidates']}
    new = {c['candidate_id']: c for c in successor['candidates']}
    selected = set(selected_ids)
    if not selected or not selected <= set(old) or set(new) != set(old):
        fail('local citation revision must preserve candidate membership')
    changed = {key for key in old if old[key] != new[key]}
    if changed - selected:
        fail('native successor changed an unselected candidate')
    shared_changed = (prior['inspections'] != successor['inspections']
                      or prior['empty_reason'] != successor['empty_reason'])
    return {'old_pass_id': prior['pass_id'], 'new_pass_id': successor['pass_id'],
            'changed_candidate_ids': sorted(changed),
            'shared_inspection_or_limit_changed': shared_changed,
            'domain_affected_candidate_ids': sorted(old if shared_changed else changed),
            'identity_affected_candidate_ids': sorted(old),
            'native_provenance_verified': False, 'reuse_approved': False}


def preview(service, workflow, selections):
    """Trace canonical references in active source reductions and draft runs.

    Selections identify facts to investigate, not confirmed mistakes. All
    candidates in the predecessor Pass remain structurally affected, including
    otherwise disjoint pages. No title matching or cN/pN alias inference is used.
    """
    k = service.knowledge
    batch = k._batch(workflow['batch_id'])
    if not selections or len(selections) > 128:
        fail('select 1..128 exact citation predecessors')
    if len({s['task_id'] for s in selections}) != len(selections):
        fail('select each task once')
    pins = {}

    def pin(path):
        path = _vault_path(service.vault, path) if isinstance(path, str) else path
        ref = path.relative_to(service.vault).as_posix()
        pins[ref] = hashlib.sha256(path.read_bytes()).hexdigest()
        return _load_json(path)

    pin(k._batch_path(batch['batch_id']))
    for outcome in workflow.get('source_outcomes', []):
        path = _vault_path(service.vault, outcome['path'])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != outcome['content_sha256']:
            fail('original source bytes changed')
        pins[outcome['path']] = digest
        for ref in outcome['artifact_refs']:
            pins[ref] = hashlib.sha256(_vault_path(service.vault, ref).read_bytes()).hexdigest()
    selected_keys, all_keys, targets = set(), set(), []
    for selection in selections:
        tid = selection['task_id']
        if tid not in batch['task_ids']:
            fail('task is outside this batch')
        task = pin(k._task_path(tid))
        if task['actor'] != workflow['actor']:
            fail('citation task actor differs')
        index, by_task = k._pass_index([tid])
        records = by_task.get(tid, [])
        if not records:
            fail('citation predecessor is missing')
        prior = max(records, key=lambda r: r['sequence'])
        if prior['pass_kind'] != 'citation' or prior['pass_id'] != selection['pass_id']:
            fail('pin the current citation predecessor')
        # File naming is owned by the repository, not inferred from sequence.
        files = [p for p in (service.vault / f'_system/knowledge-builds/task-{tid}/passes').glob('*.json')
                 if _load_json(p).get('pass_id') == prior['pass_id']]
        if len(files) != 1:
            fail('citation predecessor has no unique repository record')
        pin(files[0])
        if fingerprint({key: v for key, v in prior.items() if key != 'pass_id'}) != prior['pass_id']:
            fail('citation predecessor fingerprint changed')
        package = pin(f'_system/knowledge-builds/task-{tid}/readings/{prior["reading_package_id"]}.json')
        k._reading_package_fingerprint(package, prior['reading_package_id'])
        if (package['window']['core_refs'] != task['target_refs']
                or prior['reading_window_id'] != package['window']['window_id']
                or package['actor'] != workflow['actor'] or prior['actor'] != workflow['actor']
                or package['document_registry_revision'] != batch['document_registry_revision']):
            fail('citation reading window differs from task ownership')
        candidates = {c['candidate_id']: c for c in prior['candidates']}
        chosen = selection.get('candidate_ids', [])
        if not chosen or len(chosen) != len(set(chosen)) or not set(chosen) <= set(candidates):
            fail('select current unique candidate IDs, not display aliases')
        selected_keys.update((prior['pass_id'], cid) for cid in chosen)
        all_keys.update((prior['pass_id'], cid) for cid in candidates)
        if any((prior['pass_id'], cid) not in index for cid in candidates):
            fail('predecessor is already superseded')
        targets.append({'task_id': tid, 'pass_id': prior['pass_id'],
                        'reading_package_id': prior['reading_package_id'],
                        'selected_candidate_ids': sorted(chosen),
                        'all_candidate_ids': sorted(candidates),
                        'support_sha256': {cid: fingerprint(candidates[cid]['support_refs']) for cid in chosen},
                        'source_targets_sha256': fingerprint(task['target_refs'])})

    def touched(refs, keys):
        return bool({candidate_key(r) for r in refs} & keys)

    sources, proposals = [], []
    for rid in batch.get('resource_reduction_ids', []):
        record = pin(k._resource_reduction_path(batch['batch_id'], rid))
        validate_record('resource_reduction', record)
        if record['batch_id'] != batch['batch_id'] or record['actor'] != workflow['actor']:
            fail('source reduction ownership differs')
        ids = {r['pass_id'] for r in record['pass_refs']}
        affected = bool(ids & {pid for pid, _ in all_keys})
        sources.append({'reduction_id': rid, 'resource_id': record['resource_id'],
                        'requires_revalidation': affected})
        for proposal in record['proposals']:
            refs = proposal['candidate_refs']
            if touched(refs, all_keys):
                proposals.append({'reduction_id': rid, 'proposal_id': proposal['proposal_id'],
                                  'selected_candidate_dependency': touched(refs, selected_keys),
                                  'predecessor_identity_dependency': True,
                                  'candidate_refs': copy.deepcopy(refs)})
    coordination_id = batch.get('global_reduction_id')
    if coordination_id:
        global_record = pin(k._global_reduction_path(batch['batch_id'], coordination_id))
        validate_record('global_reduction', global_record)
        if (set(global_record['resource_reduction_ids']) != set(batch['resource_reduction_ids'])
                or set(global_record['run_ids']) != set(batch['run_ids'])):
            fail('global dependency set differs from the active batch')
    pages = []
    for run_id in batch.get('run_ids', []):
        run = pin(k._run_path(run_id))
        validate_record('build_run', run)
        if run['actor'] != workflow['actor'] or run['state'] != 'draft':
            fail('only draft runs can be considered for pre-Build amendments')
        for decision in run['decisions']:
            refs = decision['candidate_refs']
            pages.append({'run_id': run_id, 'path': decision['path'],
                          'candidate_refs': copy.deepcopy(refs),
                          'selected_candidate_dependency': touched(refs, selected_keys),
                          'predecessor_identity_dependency': touched(refs, all_keys),
                          'reuse_approved': False})
    deferred = []
    from .reduce_coverage import active
    coverage = active(service.vault, workflow)
    if coverage:
        manifest = coverage['manifest']
        pins[coverage['ref']['authorization_ref']] = coverage['ref']['authorization_sha256']
        pins[coverage['ref']['manifest_ref']] = coverage['ref']['manifest_sha256']
        source = pin(manifest['source_snapshot_ref'])
        if pins[manifest['source_snapshot_ref']] != manifest['source_snapshot_sha256']:
            fail('limited scope source snapshot changed')
        for gap in manifest['gaps']:
            refs = [ref for handle in gap['candidate_refs'] for ref in source['backend']['mapping'][handle]]
            deferred.append({'path': gap['path'], 'candidate_refs': refs,
                             'selected_candidate_dependency': touched(refs, selected_keys),
                             'predecessor_identity_dependency': touched(refs, all_keys),
                             'completed': False})
    # Every active source and aggregate is pinned, including disjoint records:
    # the report is a comparison baseline, never authority for future reuse.
    result = {'contract': CONTRACT, 'workflow_id': workflow['workflow_id'],
              'workflow_revision': workflow['revision'], 'batch_id': batch['batch_id'],
              'targets': targets, 'sources': sources, 'proposals': proposals, 'pages': pages,
              'deferred_pages': deferred,
              'aggregate_requires_revalidation': bool(coordination_id and any(s['requires_revalidation'] for s in sources)),
              'global_reduction_id': coordination_id, 'pins': pins,
              'native_calls_granted': 0, 'applied': False, 'reuse_approved': False,
              'consumed_revision_execution_supported': False,
              'required_next_steps': ['native successor provenance and bounded delta',
                  'explicit withdrawal of old acceptance before any mutation',
                  'atomic dependency retirement and stopped old executor bindings',
                  'current full-input proof before reusing any draft',
                  'bounded native regeneration of affected results only']}
    result['impact_sha256'] = fingerprint(result)
    return result
