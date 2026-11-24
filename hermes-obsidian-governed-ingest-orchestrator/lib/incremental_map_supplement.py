"""Recover one unconsumed Map packet after its original run closed."""
import json
import os
from pathlib import Path

from hermes_source_units.incremental_control import (IncrementalControl, SUPPLEMENT_AUTOMATION,
    SUPPLEMENT_AUTOMATION_CONTRACT, SUPPLEMENT_AUTOMATIC_STAGES)
from hermes_source_units.incremental_ingest import CONTRACTS, MAP_SUPPLEMENT
from hermes_source_units.incremental_sources import pinned, read_pin, require, safe_id
from hermes_source_units.source_units import _exclusive_lock, _load_json, _vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from incremental_native import service, verified_result
from incremental_runtime import (commit_reduced_map, initialize_scope, request_path,
                                 settle_usage, source_alias, source_pin)
from incremental_source_retry import recovery_inventory
from short_evidence_pass import (MAP_CONTRACT, MAP_RECEIPT, checked_answer, load_sources, map_requests,
                                 mapping_groups, seed_requests)
from short_evidence_reduce import reduce_mappings, repair_requests

REQUEST = {'run_id', 'actor', 'operation_id', 'run_revision', 'control_revision', 'source_path',
           'source_sha256', 'request_ids'}


def release_baseline(vault):
    finalize = FileVaultFinalizeService(vault)
    release_id = finalize.status()['current_release_id']
    if release_id is None:
        return None
    finalize.validate(release_id)
    manifest = finalize._manifest_path(release_id)
    return {'release_id': release_id, 'manifest': pinned(vault, manifest.relative_to(vault).as_posix())}


def downstream_boundary(domain, run):
    lineage, _ = domain.map_supplement_lineage(run)
    source = run['sources'][lineage['request']['source_path']]
    if source['state'] != 'mapped':
        return None
    evidence = {'map_result_id': source['map_result_id'], 'map_supplement': run['map_supplement'],
                'release_baseline': lineage['release_baseline']}
    return {'scope': {'scope_kind': 'source', 'scope_id': source['snapshot_id']}, 'stage': 'mapping',
            'input_fingerprint': fingerprint(evidence), 'evidence': evidence}


def check_downstream_release(domain, run, boundary, expected_release=None):
    require(boundary and boundary == downstream_boundary(domain, run),
            'Map supplement mapping checkpoint changed', 'STALE_INPUT')
    current = release_baseline(domain.vault)
    authorized = boundary['evidence']['release_baseline'] if expected_release is None else expected_release
    require(current == authorized,
            'Map supplement publication baseline changed; review a current release before continuing', 'STALE_INPUT')
    return current


def automation_authority(domain, run, head):
    """Return the immutable publication scope that authorized an automatic child."""
    lineage, _ = domain.map_supplement_lineage(run)
    pointer = head.get('supplement_automation')
    if pointer:
        grant = read_pin(domain.vault, pointer)
        request = grant['request']
        path = domain.root(run['run_id']) / 'map-supplement-automation' / (safe_id(request['operation_id']) + '.json')
        require(pointer['ref'] == path.relative_to(domain.vault).as_posix()
                and grant['contract'] == SUPPLEMENT_AUTOMATION_CONTRACT and grant['map_supplement'] == run['map_supplement']
                and request['run_id'] == run['run_id'] and request['actor'] == run['actor']
                and request['execution_scope'] == SUPPLEMENT_AUTOMATION
                and request['boundary_fingerprint'] in head['released']
                and grant['map_result_id'] in run['map_result_ids']
                and grant['release_baseline'] == lineage['release_baseline'],
                'supplement automation authorization changed', 'STALE_INPUT')
        if grant['round_proof']:
            read_pin(domain.vault, grant['round_proof'])
    elif lineage['request'].get('execution_scope') != SUPPLEMENT_AUTOMATION:
        return None
    else:
        require(lineage['authorized_stages'] == list(SUPPLEMENT_AUTOMATIC_STAGES),
                'supplement automatic stage scope changed', 'STALE_INPUT')
    require(not head['pause_after'], 'automatic supplement planned checkpoints changed', 'STALE_INPUT')
    return {key: lineage[key] for key in ('release_baseline', 'provider_config')}


def remaining_budget(parent):
    budget = parent['budget']
    spent = dict.fromkeys(('calls', 'input_tokens', 'output_tokens'), 0)
    phase_calls = dict.fromkeys(budget['phase_limits'], 0)
    for row in parent['reservations'].values():
        actual = row['actual'] if row['state'] == 'settled' else {
            field: row['max_' + field] for field in spent}
        for field in spent:
            spent[field] += actual[field]
        phase_calls[row['phase']] += actual['calls']
    remaining = {'max_' + field: budget['max_' + field] - used for field, used in spent.items()}
    limits = {phase: cap - phase_calls[phase] for phase, cap in budget['phase_limits'].items()}
    require(all(value >= 0 for value in (*remaining.values(), *limits.values())) and limits.get('mapping', 0) > 0,
            'original run has no remaining Map supplement budget', 'BUDGET_EXHAUSTED')
    return {**remaining, 'phase_limits': {**limits, 'mapping': 1}}


def original_inputs(domain, parent, lineage):
    """Recreate the original request set from current governed, hash-pinned inputs."""
    path = lineage['request']['source_path']
    snapshot_id = lineage['snapshot_id']
    snapshot = domain.validate_source_snapshot(parent['run_id'], snapshot_id)
    sources = load_sources(domain.vault, [source_pin(snapshot)],
                           aliases={snapshot['identity']['resource_id']: source_alias(parent, snapshot)},
                           actor=parent['actor'])
    for item in sources.values():
        item['incremental_snapshot_id'] = snapshot_id
    jobs, receipts = {}, {}
    for row in lineage['completed']:
        identifier = row['request_id']
        jobs[identifier] = read_pin(domain.vault, row['proof']['request'])['request']
        receipts[identifier] = read_pin(domain.vault, row['receipt'])
        checked_answer(jobs[identifier], receipts[identifier])
    selected = read_pin(domain.vault, lineage['selected']['job_pin'])['request']
    seeds = seed_requests(sources)
    require(all(jobs.get(row['id']) == row for row in seeds), 'original topic seeds differ from the current source snapshot')
    mapping = map_requests(sources, mapping_groups(sources, MAP_CONTRACT), seeds, receipts, MAP_CONTRACT)
    by_id = {row['id']: row for row in mapping}
    require(by_id.get(selected['id']) == selected and selected['id'] not in jobs
            and all(row == selected or jobs.get(row['id']) == row for row in mapping),
            'original required Map packets are incomplete or differ from the current source snapshot', 'SOURCE_CHANGED')
    reused_repairs = [row for row in jobs.values() if row['view'].get('issue')]
    original_rows = [row for row in mapping if row['id'] != selected['id']]
    expected_repairs = {row['id']: row for row in repair_requests(sources, original_rows, receipts, MAP_CONTRACT)}
    require(all(expected_repairs.get(row['id']) == row for row in reused_repairs)
            and set(jobs) == {row['id'] for row in seeds + original_rows + reused_repairs},
            'original Map companion receipt is outside its deterministic request set')
    return sources, mapping, reused_repairs, receipts, selected


def supplement_report(domain, request, child_id):
    parent_id = request['run_id']
    parent = domain.status(parent_id)
    control = IncrementalControl(domain.vault, parent_id)
    head = control.status()
    require(parent['state'] in ('active', 'partial') and 'map_supplement' not in parent
            and head['state'] == 'active'
            and parent['revision'] == request['run_revision'] and head['revision'] == request['control_revision']
            and parent['actor'] == request['actor'] == head['actor'],
            'Map supplement requires an exact active or partially closed parent', 'STALE_INPUT')
    inventory = recovery_inventory(domain, parent_id, request['source_path'], request_ids=request['request_ids'])
    candidates = inventory['eligible']
    require(inventory['mode'] == 'supplemental' and not inventory['native_owner']
            and inventory['source_sha256'] == request['source_sha256'] and len(candidates) == 1
            and candidates[0]['request_id'] == request['request_ids'][0] and candidates[0]['phase'] == 'short-map'
            and candidates[0]['physical_sends'] == 0, 'no exact unconsumed primary Map packet is eligible')
    selected = candidates[0]
    source = parent['sources'][request['source_path']]
    require(source['state'] == 'mapping_held', 'Map supplement requires a durable source hold')
    if parent['active_round_id']:
        frozen = domain.round_input(parent_id, parent['active_round_id'])
        require(source['snapshot_id'] not in frozen['snapshots'],
                'Map supplement source already belongs to the original frozen round')
    original_hold = source['mapping_hold']['execution_hold']
    hold = read_pin(domain.vault, original_hold)
    require(hold['reason'] == 'required-map-incomplete' and hold['board'] == inventory['board']
            and {row['task_id'] for row in hold['jobs']} == {selected['task_id']},
            'Map supplement requires one exact required packet from the sealed source hold')
    parent_root = domain.root(parent_id)
    original_job = _vault_path(domain.vault, inventory['workspace']) / 'jobs' / selected['job']
    job_pointer = pinned(domain.vault, original_job.relative_to(domain.vault).as_posix())
    require(job_pointer['sha256'] == selected['job_sha256']
            and any(row['job'] == job_pointer and row['state'] == 'unconfirmed' for row in hold['jobs']),
            'Map supplement request differs from the original frozen hold')
    runtime = _load_json(request_path(domain, parent_id))
    provider_config = Path(runtime['provider_config']).resolve(strict=True)
    from run_short_evidence_test import sha

    report = {'contract': MAP_SUPPLEMENT, 'request': request, 'supplement_run_id': child_id,
              'snapshot_id': inventory['snapshot_id'], 'original_hold': original_hold,
              'parent_run': pinned(domain.vault, (parent_root / 'run.json').relative_to(domain.vault).as_posix()),
              'parent_control': pinned(domain.vault, control.path.relative_to(domain.vault).as_posix()),
              'parent_closure': parent.get('closure'), 'parent_start_fingerprint': parent['start_fingerprint'],
              'parent_source': {key: source[key] for key in ('snapshot', 'seal')},
              'selected': {**selected, 'job_pin': job_pointer},
              'completed': inventory['completed'], 'budget': remaining_budget(parent),
              'release_baseline': release_baseline(domain.vault),
              'provider_config': {'path': str(provider_config), 'sha256': sha(provider_config)}}
    if request.get('execution_scope') == SUPPLEMENT_AUTOMATION:
        report['authorized_stages'] = list(SUPPLEMENT_AUTOMATIC_STAGES)
    _, _, _, _, row = original_inputs(domain, parent, report)
    reservation = fingerprint({'request': row['id'], 'ordinal': 1})
    claim = _vault_path(domain.vault,
        '_system/metadata/incremental-work-budgets/' + fingerprint(reservation) + '.json')
    if claim.is_file():
        require(_load_json(claim).get('run_id') == child_id,
                'the exact original request already owns a semantic budget', 'SEMANTIC_REUSE_REQUIRED')
    require(row['budget']['fits'] and report['budget']['max_calls'] >= 1
            and report['budget']['max_input_tokens'] >= (row['budget'].get('text_tokens') or row['budget']['utf8_bytes'])
            and report['budget']['max_output_tokens'] >= row['output_cap'],
            'original run has insufficient budget for its exact Map packet', 'BUDGET_EXHAUSTED')
    return report


def run_packet(domain, run, lineage, parent):
    import run_short_evidence_test as native
    from incremental_recovery import pending_request, request_files

    run_id, snapshot_id = run['run_id'], lineage['snapshot_id']
    root, operation = initialize_scope(domain, run_id, 'source', snapshot_id, 'mapping', [snapshot_id],
                                       map_supplement=run['map_supplement'])
    sources, mapping, repairs, responses, selected = original_inputs(domain, parent, lineage)
    status = run['sources'][lineage['request']['source_path']]
    if status['state'] == 'mapped':
        return domain.read_map(run_id, status['map_result_id'])
    job, _ = request_files(root, selected)
    if pending_request(root, operation, selected):
        from incremental_map_recovery import job_ordinals, ordinal_reservations

        require(not job_ordinals(domain.root(run_id), selected, ordinal_reservations(domain.root(run_id))),
                'supplement packet already owns a model reservation; no automatic resend')
        original = recovery_inventory(domain, parent['run_id'], lineage['request']['source_path'],
                                      request_ids=[selected['id']])
        selected_proof = {key: value for key, value in lineage['selected'].items() if key != 'job_pin'}
        require(not original['native_owner'] and original['control_state'] == 'active'
                and original['eligible'] == [selected_proof] and original['completed'] == lineage['completed'],
                'original no-send proof or companion receipts changed before native dispatch', 'STALE_INPUT')
        native.stage(root, operation, [selected], 1)
    require(job.with_suffix('.result.json').is_file(), 'supplement packet has no acknowledged native result; no resend')
    result, proof = verified_result(root, selected)
    settle_usage(domain, run_id, root, selected, result)
    require(result['state'] == 'settled', 'supplement packet was held; retained original consumption evidence')
    receipt_path = root / 'receipts' / job.name
    receipt = pinned(domain.vault, receipt_path.relative_to(domain.vault).as_posix())
    responses[selected['id']] = read_pin(domain.vault, receipt)
    reduced = reduce_mappings(sources, mapping, responses, repairs)
    map_ids = {row['id'] for row in mapping + repairs}
    versioned = {identifier: {'issues': value['issues'], 'receipt_sha256': value['receipt_sha256']}
                 for identifier, value in responses.items() if identifier in map_ids and value['contract'] == MAP_RECEIPT}
    if versioned:
        reduced['mapping_receipts'] = versioned
    proofs = {row['request_id']: (row['proof'], row['receipt']) for row in lineage['completed']}
    proofs[selected['id']] = proof, receipt
    ordered = [proofs[identifier] for identifier in sorted(proofs)]
    completions = []
    for binding, _ in ordered:
        original = _vault_path(domain.vault, binding['request']['ref'])
        completions.append({'proof': binding, 'task_body': json.loads(native.task_body(original.parent.parent, original))})
    return commit_reduced_map(domain, run_id, snapshot_id, root, sources, reduced,
                              completions, [pointer for _, pointer in ordered])


def supplement(vault, request, *, preview=False):
    """Recover the exact Map packet within a separately authorized child run."""
    from prepare_short_evidence_test import save

    optional = {'execution_scope'} & request.keys()
    require(set(request) == REQUEST | optional | (set() if preview else {'expected_report_sha256'})
            and (not optional or request['execution_scope'] == SUPPLEMENT_AUTOMATION),
            'exact Map supplement operator request required', 'INVALID_SCHEMA')
    parent_id = safe_id(request['run_id'])
    safe_id(request['operation_id'])
    selected = request['request_ids']
    require(isinstance(selected, list) and len(selected) == 1 and safe_id(selected[0]),
            'Map supplement requires one exact original request ID')
    request_body = {key: request[key] for key in REQUEST | optional}
    automatic = request_body.get('execution_scope') == SUPPLEMENT_AUTOMATION
    child_id = 'map-supplement-' + fingerprint([parent_id, request['source_path'], selected[0]])[:48]
    domain = service(Path(vault), child_id)
    root = domain.root(child_id)
    report_path = root / 'map-supplement.json'
    if preview:
        report = _load_json(report_path) if report_path.is_file() else supplement_report(domain, request_body, child_id)
        require(report['request'] == request_body, 'Map supplement identity is already reserved')
        return {**report, 'report_sha256': fingerprint(report), 'read_only': True}
    require(not any(os.environ.get(key) for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB',
                                                     'HERMES_DELEGATED_CHILD_CONTEXT')),
            'Map supplement requires a trusted operator')
    with _exclusive_lock(root / '.controller.lock', timeout=0, marker=False):
        prior = _load_json(report_path) if report_path.is_file() else None
        if prior:
            require(prior['request'] == request_body and fingerprint(prior) == request['expected_report_sha256'],
                    'Map supplement authorization changed', 'STALE_INPUT')
        result_path = root / 'map-supplement-result.json'
        if result_path.is_file():
            return _load_json(result_path)
        started = (root / 'run.json').is_file()
        report = prior if started else supplement_report(domain, request_body, child_id)
        require(report and fingerprint(report) == request['expected_report_sha256'] and (not prior or prior == report),
                'Map supplement preview changed', 'STALE_INPUT')
        save(report_path, report)
        binding = pinned(domain.vault, report_path.relative_to(domain.vault).as_posix())
        parent = domain.status(parent_id)
        head = IncrementalControl(domain.vault, parent_id).status()
        pauses = [] if automatic else ['mapping', 'writing', *[stage for stage in head['pause_after']
                                                               if stage not in ('prepare', 'mapping', 'writing')]]
        control = IncrementalControl(domain.vault, child_id)
        control.initialize(request['actor'], pauses)
        domain.start({'workflow_mode': 'incremental_v1', 'run_id': child_id,
                      'operation_id': request['operation_id'], 'actor': request['actor'],
                      'sources': [{'path': request['source_path'], 'sha256': request['source_sha256']}],
                      'source_workflow_id': parent['source_workflow_id'], 'contracts': CONTRACTS,
                      'policy': {'max_maps_per_round': 1}, 'budget': report['budget'],
                      'control_ref': control.path.relative_to(domain.vault).as_posix(), 'map_supplement': binding})
        save(request_path(domain, child_id), {'provider_config': report['provider_config']['path']})
        run = domain.status(child_id)
        snapshot_id = report['snapshot_id']
        scope = {'scope_kind': 'source', 'scope_id': snapshot_id}
        control.reconcile_executors()
        if control.status()['state'] == 'active':
            with control.executing(scope, 'mapping', fingerprint(report)):
                run_packet(domain, run, report, parent)
        run = domain.status(child_id)
        boundary = downstream_boundary(domain, run)
        require(boundary, 'supplement did not seal its authorized Map')
        head = control.checkpoint(scope, 'mapping', boundary['evidence'])
        if not automatic:
            require(head['state'] == 'paused' and head['boundary'] == boundary,
                    'separate downstream authorization checkpoint was not recorded')
        result = {'run_id': parent_id, 'supplement_run_id': child_id, 'report_sha256': fingerprint(report),
                  'map_result_id': boundary['evidence']['map_result_id'], 'reused_receipts': len(report['completed']),
                  'state': 'map-completed' if automatic else 'mapping-paused',
                  'run_revision': run['revision'], 'control_revision': head['revision'],
                  'boundary_fingerprint': fingerprint(boundary)}
        if automatic:
            result['execution_scope'] = SUPPLEMENT_AUTOMATION
        save(result_path, result)
        return result
