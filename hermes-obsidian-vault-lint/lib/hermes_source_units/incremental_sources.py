"""Content identities and independently verified native evidence for incremental ingest."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re

from .source_units import ARTIFACT_ROOT, UNIT_ROOT, FileSourceUnitService, _load_json, _sha, _vault_path
from .validation import ContractError, fingerprint


def require(condition, message, code='INCREMENTAL_INVALID'):
    if not condition:
        raise ContractError(code, '$', message)


def safe_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}', value),
            'invalid incremental operation identity')
    return value


def pinned(vault, ref):
    path = _vault_path(vault, ref)
    require(path.is_file() and not path.is_symlink(), 'missing immutable evidence: ' + ref)
    return {'ref': ref, 'sha256': _sha(path.read_bytes())}


def read_pin(vault, pointer):
    path = _vault_path(vault, pointer['ref'])
    require(path.is_file() and _sha(path.read_bytes()) == pointer['sha256'],
            'pinned evidence changed', 'SOURCE_CHANGED')
    return _load_json(path)


def completion_connection(board):
    """Read an existing historical board without a worker's own DB override.

    Hermes injects HERMES_KANBAN_DB into workers, so a board argument alone
    still selects the worker's current board. Use the public board layout and
    pass an explicit existing database to the normal connection entry point;
    its delegation fences and read-only handling remain authoritative.
    """
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc

    safe_id(board)
    require(kb.board_exists(board), 'native evidence board is missing', 'NATIVE_UNCONFIRMED')
    directory = kb.kanban_home() if board == 'default' else kb.board_dir(board)
    path = directory / 'kanban.db'
    require(not directory.is_symlink() and path.is_file() and not path.is_symlink()
            and path.resolve(strict=True).parent == directory.resolve(strict=True),
            'native evidence database is missing or redirected', 'NATIVE_UNCONFIRMED')
    return kc.connect(db_path=path.resolve(strict=True), board=board)


def verify_native_completion(vault, proof, expected):
    """Check a saved native run, never a worker's declaration of completion."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc

    require(set(('board', 'task_id', 'run_id')) <= set(proof)
            and type(proof['run_id']) is int, 'exact native completion identity required')
    with closing(completion_connection(proof['board'])) as conn:
        task = kb.get_task(conn, proof['task_id'])
        require(task and task.status in ('done', 'archived') and not task.claim_lock,
                'native task has not completed', 'NATIVE_UNCONFIRMED')
        runs = kb.list_runs(conn, task.id, include_active=False)
        require(any(run.id == proof['run_id'] and run.outcome == 'completed' for run in runs),
                'native completed run is missing', 'NATIVE_UNCONFIRMED')
    body = json.loads(task.body)
    if expected.get('source_workflow_id'):
        workflow_id = expected['source_workflow_id']
        workflow = _load_json(_vault_path(vault, f'_system/ledgers/ingest-workflows/{workflow_id}.json'))
        node = 'source-prepare:' + fingerprint({'path': expected['source_path'],
                                               'content_sha256': expected['source_sha256']})[:16]
        require(workflow['kanban']['board_id'] == proof['board'] and body.get('workflow_id') == workflow_id
                and body.get('node') == node and body.get('vault') == str(vault)
                and any(row['node'] == node and row['task_id'] == proof['task_id']
                        for row in workflow['kanban']['task_map']), 'source native identity differs')
    else:
        expected_body = expected.get('task_body')
        require(expected_body is not None and body == expected_body,
                'semantic task must bind the exact immutable request')
        for name in ('request', 'result'):
            require(name in proof, 'native result and request pointers required')
            read_pin(vault, proof[name])
        result = read_pin(vault, proof['result'])
        require(result.get('task_id') == proof['task_id'] and result.get('run_id') == proof['run_id'],
                'native completion result belongs to another task/run')
        job = _vault_path(vault, proof['request']['ref'])
        request = read_pin(vault, proof['request'])
        operation = _load_json(job.parent.parent / 'operation.json')
        require(body.get('job') == job.name and body.get('job_sha256') == proof['request']['sha256']
                and body.get('operation_sha256') == _sha((job.parent.parent / 'operation.json').read_bytes())
                and body.get('contract') == operation.get('contract')
                and operation.get('workflow_mode') == 'incremental_v1'
                and operation['incremental']['run_id'] == expected.get('run_id')
                and result.get('request_id') == request['request']['id'], 'native job is outside this incremental scope')
        if expected.get('snapshot_id'):
            require(expected['snapshot_id'] in operation['incremental']['snapshots'],
                    'native request did not admit this source snapshot')
    return {'board': proof['board'], 'task_id': proof['task_id'], 'run_id': proof['run_id'],
            'task_body_sha256': fingerprint(body)}


def verify_native_drain(vault, proof, source_workflow_id, boards):
    """Recheck current queues and actual consumers; a past completion is insufficient."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc
    from native_stop_inventory import workflow_processes

    require(proof.get('source_workflow_id') == source_workflow_id and set(proof.get('boards', [])) == set(boards),
            'native drain scope differs')
    for board in boards:
        with closing(completion_connection(board)) as conn:
            require(not any(task.status in ('ready', 'running', 'todo', 'triage') or task.claim_lock
                            for task in kb.list_tasks(conn)), 'native board has unfinished work', 'NATIVE_ACTIVE')
        require(not workflow_processes(source_workflow_id, board), 'native processes remain active', 'NATIVE_ACTIVE')
    return {'source_workflow_id': source_workflow_id, 'boards': sorted(set(boards)), 'verified': True}


def snapshot_content(vault, actor, original, outcome):
    """Verify one current source without waiting for unrelated source workers."""
    require(outcome.get('status') == 'ready' and outcome.get('path') == original['path']
            and outcome.get('content_sha256') == original['sha256'], 'ready source outcome differs')
    require(_sha(_vault_path(vault, original['path']).read_bytes()) == original['sha256'],
            'original source changed', 'SOURCE_CHANGED')
    source = FileSourceUnitService(vault)
    resource, unit_set = outcome['resource_id'], outcome['unit_set_id']
    source.validate(resource, unit_set)
    current = source._current(resource)
    require(current and current['unit_set_id'] == unit_set, 'source version was replaced', 'STALE_INPUT')
    manifest, units, _ = source._load_set(resource, unit_set)
    require(units and all(row['source_sha256'] == original['sha256'] for row in units), 'UnitSet source differs')
    artifact_ref = f"{ARTIFACT_ROOT}/{resource}/{manifest['artifact_revision']}/manifest.json"
    artifact = _load_json(_vault_path(vault, artifact_ref))
    registry = _load_json(_vault_path(vault, '_system/metadata/document-registry.json'))
    source._verify_governance(artifact, {'actor': actor, 'purpose': 'construction',
                                          'registry_revision': registry['registry_revision']})
    record = next(row for row in registry['records'] if row.get('version_id') == manifest['identity']['version_id'])
    require(record.get('governance_status') not in ('withdrawn', 'revoked', 'rejected', 'superseded', 'retired'),
            'source governance no longer permits construction', 'ACCESS_DENIED')
    bundle_candidates = [ref for ref in outcome.get('artifact_refs', [])
                         if ref.endswith('/manifest.json') and ref.startswith(('_system/reports/source-bundles/', '10_Raw/converted/'))]
    # Reused UnitSets may return only their manifest. Resolve the registered,
    # hash-identical source Bundle instead of invoking conversion again.
    if not bundle_candidates:
        for base in ('_system/reports/source-bundles', '10_Raw/converted'):
            for path in _vault_path(vault, base).rglob('manifest.json'):
                value = _load_json(path)
                if value.get('source', {}).get('sha256') == original['sha256'] and all(
                        value.get('governance', {}).get(key) == expected for key, expected in manifest['identity'].items()):
                    bundle_candidates.append(path.relative_to(vault).as_posix())
    verified = []
    for ref in sorted(set(bundle_candidates)):
        path = _vault_path(vault, ref)
        bundle = _load_json(path)
        governance = bundle.get('governance', {})
        if (bundle.get('source', {}).get('sha256') != original['sha256']
                or any(governance.get(key) != value for key, value in manifest['identity'].items())):
            continue
        inventory = {}
        for child in sorted(path.parent.rglob('*')):
            require(not child.is_symlink() and child.resolve().is_relative_to(vault), 'unsafe Bundle member')
            if child.is_file():
                inventory[child.relative_to(path.parent).as_posix()] = _sha(child.read_bytes())
        require('manifest.json' in inventory, 'Bundle manifest is absent')
        verified.append({'manifest_ref': ref, 'files': inventory})
    require(verified or _vault_path(vault, original['path']).suffix.lower() not in ('.pdf',),
            'source Bundle provenance is missing')
    value = {'contract': 'incremental-source-snapshot/v1', 'identity': manifest['identity'],
             'original': {'ref': original['path'], 'sha256': original['sha256']},
             'unit_set': {'id': unit_set, **pinned(vault, f'{UNIT_ROOT}/{resource}/{unit_set}/manifest.json')},
             'artifact': {'revision': manifest['artifact_revision'], **pinned(vault, artifact_ref)},
             'bundle': verified[0] if verified else None,
             'units': [{'unit_ref': row['ref'], 'sha256': row['content_sha256']} for row in units]}
    value['snapshot_id'] = 'snapshot-' + fingerprint(value)
    return value, registry['registry_revision']
