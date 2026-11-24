"""A directed supplement creates only one new native packet."""
from pathlib import Path
import sys

from test_p3_knowledge_build import vault


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hermes-obsidian-governed-ingest-orchestrator/scripts'))


def test_directed_map_supplement_preserves_parent_and_replays_confirmation(vault, monkeypatch):
    import incremental_native
    import incremental_map_supplement as supplement
    import run_short_evidence_test as native
    from hermes_source_units.incremental_control import IncrementalControl
    from hermes_source_units.incremental_ingest import CONTRACTS, FileIncrementalIngestService
    from hermes_source_units.incremental_sources import pinned, read_pin
    from hermes_source_units.source_units import _json_bytes

    source_path = '10_Raw/selected.pdf'
    source_bytes = b'fixture source bytes'
    original = vault / source_path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(source_bytes)
    source_pin = pinned(vault, source_path)
    snapshot = 'snapshot-test'
    parent_id = 'parent'
    actor = 'builder'

    def put(path, record):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_json_bytes(record))
        return pinned(vault, path.relative_to(vault).as_posix())

    def native_proof(producer, row, task):
        job = producer / 'jobs' / (row['id'] + '.json')
        request = put(job, {'request': row})
        result = put(job.with_suffix('.result.json'), {
            'state': 'settled', 'request_id': row['id'], 'task_id': task, 'run_id': 7, 'attempts': [],
        })
        receipt = put(producer / 'receipts' / job.name, {'contract': 'fixture'})
        return {'board': task + '-board', 'task_id': task, 'run_id': 7, 'request': request, 'result': result}, receipt

    verified = []

    def native_completion(_vault, proof, expected):
        read_pin(vault, proof['request'])
        read_pin(vault, proof['result'])
        verified.append((proof['task_id'], expected['run_id']))
        return {k: proof[k] for k in ('board', 'task_id', 'run_id')}

    def service(_vault, run_id):
        control = IncrementalControl(vault, run_id)
        return FileIncrementalIngestService(vault, check_admission=control.check_admission,
                                            native_evidence=native_completion)

    monkeypatch.setattr(supplement, 'service', service)
    monkeypatch.setattr(incremental_native, 'service', service)
    monkeypatch.setattr(IncrementalControl, 'sources_active', lambda self: False)
    domain = service(vault, parent_id)
    parent = domain.root(parent_id)
    ctl = IncrementalControl(vault, parent_id)
    ctl.initialize(actor, [])
    workspace = parent / 'native/source' / snapshot / 'mapping'
    put(workspace / 'operation.json', {'contract': native.CONTRACT, 'board': 'old-board'})
    historic_row = {'id': 'a' * 64, 'phase': 'short-map', 'view': {}}
    selected_row = {'id': 'b' * 64, 'phase': 'short-map', 'view': {},
                    'budget': {'fits': True, 'text_tokens': 51}, 'output_cap': 200}
    old_proof, old_receipt = native_proof(workspace, historic_row, 'old-completed')
    original_job = put(workspace / 'jobs' / (selected_row['id'] + '.json'), {'request': selected_row})
    hold = put(workspace / 'execution-hold.json', {
        'reason': 'required-map-incomplete', 'board': 'old-board',
        'jobs': [{'task_id': 'old-blocked', 'job': original_job, 'state': 'unconfirmed'}],
    })
    resources = {'S1': {'items': {
        'A': {'source_ref': {'unit_ref': {'ref': 'unit-a'}}, 'sha256': 'a' * 64},
        'B': {'source_ref': {'unit_ref': {'ref': 'unit-b'}}, 'sha256': 'b' * 64},
    }}}
    snap = {'snapshot_id': snapshot, 'original': source_pin, 'units': [
        {'unit_ref': row['source_ref']['unit_ref'], 'sha256': row['sha256']}
        for row in resources['S1']['items'].values()]}
    snap_pin = put(parent / 'snapshot.json', snap)
    seal = put(parent / 'seal.json', {'snapshot': snap_pin})
    closure_pin = put(parent / 'closure.json', {'state': 'partial'})
    parent_state = {'contract': CONTRACTS['run'], 'workflow_mode': 'incremental_v1', 'run_id': parent_id,
                    'vault_id': domain.vault_id, 'actor': actor, 'revision': 7, 'state': 'partial',
                    'source_workflow_id': 'parent-workflow', 'start_fingerprint': 'bound-parent',
                    'active_round_id': None, 'closure': closure_pin, 'reservations': {},
                    'budget': {'max_calls': 4, 'max_input_tokens': 1000, 'max_output_tokens': 1000,
                               'phase_limits': {'mapping': 4}},
                    'sources': {source_path: {'state': 'mapping_held', 'snapshot_id': snapshot,
                        'snapshot': snap_pin, 'seal': seal, 'source_outcome': {},
                        'mapping_hold': {'execution_hold': hold}}}}
    put(parent / 'run.json', parent_state)
    provider = parent / 'provider-config.json'
    put(provider, {})
    put(parent / 'runtime-request.json', {'provider_config': str(provider)})
    original_files = {path: path.read_bytes() for path in parent.rglob('*.json')}
    monkeypatch.setattr(FileIncrementalIngestService, 'validate_source_snapshot', lambda *_: snap)
    monkeypatch.setattr(supplement, 'release_baseline', lambda _vault: None)
    candidate = {'request_id': selected_row['id'], 'phase': 'short-map', 'task_id': 'old-blocked',
                 'job': Path(original_job['ref']).name, 'job_sha256': original_job['sha256'], 'physical_sends': 0}
    completed = {'request_id': historic_row['id'], 'proof': old_proof, 'receipt': old_receipt}
    monkeypatch.setattr(supplement, 'recovery_inventory', lambda *args, **kwargs: {
        'mode': 'supplemental', 'native_owner': False, 'source_sha256': source_pin['sha256'],
        'snapshot_id': snapshot, 'workspace': workspace.relative_to(vault).as_posix(),
        'board': 'old-board', 'eligible': [candidate], 'completed': [completed], 'control_state': 'active',
    })
    monkeypatch.setattr(supplement, 'original_inputs', lambda *_: (
        resources, [historic_row, selected_row], [], {historic_row['id']: read_pin(vault, old_receipt)}, selected_row))
    monkeypatch.setattr(supplement, 'reduce_mappings', lambda sources, mapping, receipts, repairs: {
        'dispositions': {key: {'status': 'linked'} for key in sources['S1']['items']}, 'unresolved': [],
    })

    def scope(domain, child, kind, scope, stage, snapshots, *, map_supplement):
        root = domain.root(child) / 'native/source' / snapshot / 'mapping'
        operation = {'contract': native.CONTRACT, 'workflow_mode': 'incremental_v1', 'vault': str(vault),
                     'board': 'new-completed-board', 'map_supplement': map_supplement,
                     'incremental': {'run_id': child, 'stage': 'mapping', 'scope_kind': 'source',
                                     'scope_id': snapshot, 'snapshots': [snapshot]}}
        put(root / 'operation.json', operation)
        return root, operation

    monkeypatch.setattr(supplement, 'initialize_scope', scope)
    sends = []
    child_proof = []

    def stage(root, op, rows, capacity):
        assert op['board'] != 'old-board' and rows == [selected_row] and capacity == 1
        incremental_native.reserve_call(root, rows[0], 1, {})
        sends.append(rows[0]['id'])
        proof, _ = native_proof(root, rows[0], 'new-completed')
        child_proof.append(proof)

    monkeypatch.setattr(native, 'stage', stage)
    monkeypatch.setattr(supplement, 'verified_result', lambda *_: (read_pin(vault, child_proof[0]['result']), child_proof[0]))
    request = {'run_id': parent_id, 'actor': actor, 'operation_id': 'directed-retry', 'run_revision': 7,
               'control_revision': 1, 'source_path': source_path, 'source_sha256': source_pin['sha256'],
               'request_ids': [selected_row['id']]}
    report = supplement.supplement(vault, request, preview=True)
    child = report['supplement_run_id']
    assert not domain.root(child).exists()
    confirm = {**request, 'expected_report_sha256': report['report_sha256']}
    applied = supplement.supplement(vault, confirm)
    assert supplement.supplement(vault, confirm) == applied
    assert sends == [selected_row['id']] and applied['reused_receipts'] == 1
    assert applied['state'] == 'mapping-paused' and IncrementalControl(vault, child).status()['state'] == 'paused'
    child_map = domain.read_map(child, applied['map_result_id'])
    assert {row['producer_run_id'] for row in child_map['native_completions']} == {parent_id, child}
    assert ('old-completed', parent_id) in verified and ('new-completed', child) in verified
    assert {path: path.read_bytes() for path in parent.rglob('*.json')} == original_files
