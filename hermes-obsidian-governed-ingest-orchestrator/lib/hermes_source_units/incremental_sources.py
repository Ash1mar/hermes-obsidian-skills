"""Content identities and independently verified native evidence for incremental ingest."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3

from .source_units import ARTIFACT_ROOT, UNIT_ROOT, FileSourceUnitService, _load_json, _sha, _vault_path
from .validation import ContractError, fingerprint


MAP_ISOLATION = 'incremental-map-execution-isolation/v1'


class NativeBoardBusy(ContractError):
    def __init__(self, message):
        super().__init__('NATIVE_BOARD_BUSY', '$', message)


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


def disappeared_native_sidecar(path, error):
    """Distinguish SQLite's disappearing journal sidecar from real access denial."""
    for suffix in ('-wal', '-shm'):
        sidecar = path.with_name(path.name + suffix)
        diagnostic = f'kanban.db ({path.name}) is not writable: file {sidecar} is read-only for this user.'
        if not str(error).startswith(diagnostic):
            continue
        try:
            sidecar.lstat()
        except FileNotFoundError:
            return (path.is_file() and not path.is_symlink() and os.access(path, os.R_OK | os.W_OK)
                    and os.access(path.parent, os.R_OK | os.W_OK | os.X_OK))
    return False


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
    path = path.resolve(strict=True)
    try:
        return kc.connect(db_path=path, board=board)
    except sqlite3.OperationalError as error:
        base_code = (getattr(error, 'sqlite_errorcode', None) or 0) & 255
        if base_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or disappeared_native_sidecar(path, error):
            raise NativeBoardBusy('native board connection is temporarily unavailable: ' + str(error)) from error
        raise


def verify_mapping_isolation(vault, pointer, workspace, *, run_id, snapshot_id, actor):
    """Verify exact ended Map jobs without granting those jobs a positive receipt."""
    from hermes_cli import kanban_db as kb

    require(pointer['ref'] == (workspace / 'execution-hold.json').relative_to(vault).as_posix(),
            'mapping isolation evidence escaped the source workspace')
    report = read_pin(vault, pointer)
    operation_path = workspace / 'operation.json'
    operation = _load_json(operation_path)
    scope = operation.get('incremental', {})
    require(report.get('contract') == MAP_ISOLATION and report.get('run_id') == run_id
            and report.get('snapshot_id') == snapshot_id and report.get('board') == operation.get('board')
            and report.get('reason') in ('partial', 'seed-incomplete', 'required-map-incomplete')
            and isinstance(report.get('jobs'), list) and report['jobs']
            and operation.get('workflow_mode') == 'incremental_v1' and operation.get('vault') == str(vault)
            and operation.get('actor') == actor and scope.get('run_id') == run_id
            and scope.get('scope_kind') == 'source' and scope.get('scope_id') == snapshot_id
            and scope.get('snapshots') == [snapshot_id] and scope.get('stage') == 'mapping',
            'mapping isolation report or native scope changed')
    operation_hash = _sha(operation_path.read_bytes())
    phases, jobs, repairs = set(), set(), {}
    required_map_missing = False
    with closing(completion_connection(operation['board'])) as conn:
        for entry in report['jobs']:
            job = _vault_path(vault, entry['job']['ref'])
            row = read_pin(vault, entry['job'])['request']
            request_id = safe_id(row['id'])
            phase = row['phase']
            require(job == workspace / 'jobs' / (request_id + '.json') and request_id not in jobs
                    and phase in ('short-topics', 'short-map') and entry.get('state') in ('unconfirmed', 'held')
                    and type(entry.get('run_id')) is int, 'mapping isolation job identity differs')
            task = kb.get_task(conn, safe_id(entry['task_id']))
            body = {'contract': operation['contract'], 'operation_sha256': operation_hash, 'job': job.name,
                    'job_sha256': entry['job']['sha256']}
            require(task and task.created_by == operation['contract'] and not task.claim_lock
                    and json.loads(task.body) == body, 'mapping isolation task binding changed')
            run = next((item for item in kb.list_runs(conn, task.id, include_active=False)
                        if item.id == entry['run_id'] and item.ended_at is not None), None)
            require(run is not None, 'mapping isolation requires an ended native task', 'NATIVE_UNCONFIRMED')
            result_pointer = entry.get('result')
            result_path = job.with_suffix('.result.json')
            result = None
            if result_pointer or result_path.is_file():
                require(result_pointer and result_pointer['ref'] == result_path.relative_to(vault).as_posix(),
                        'mapping isolation native result escaped its exact job')
                result = read_pin(vault, result_pointer)
                require(result.get('task_id') == task.id and result.get('run_id') == run.id
                        and result.get('request_id') == request_id, 'mapping isolation native result identity differs')
            if entry['state'] == 'held':
                require(task.status in ('done', 'archived') and run.outcome == 'completed'
                        and result and result.get('state') == 'held', 'mapping task did not acknowledge a held result')
            else:
                require(task.status in ('blocked', 'archived') and run.outcome != 'completed',
                        'mapping isolation cannot replace an active or completed native result')
            if reference := entry.get('evidence'):
                require(_vault_path(vault, reference['ref']) == workspace / 'mapping-dispositions' / job.name,
                        'mapping job diagnostic escaped the original workspace')
                diagnostic = read_pin(vault, reference)
                require(all(diagnostic.get(key) == entry[key] for key in ('job', 'task_id', 'run_id', 'state'))
                        and diagnostic.get('native_outcome') == run.outcome,
                        'mapping job diagnostic differs from the original disposition')
            jobs.add(request_id)
            phases.add(phase)
            if phase == 'short-map':
                assigned = row['view'].get('assigned')
                require(isinstance(assigned, list) and assigned
                        and all(isinstance(alias, str) for alias in assigned), 'isolated Map job lacks assigned units')
                issue = row['view'].get('issue')
                if issue is None:
                    required_map_missing = True
                else:
                    require(isinstance(issue, dict) and assigned == [issue.get('item')]
                            and isinstance(issue.get('reason'), str) and issue['reason']
                            and safe_id(issue.get('parent_request')) and assigned[0] not in repairs,
                            'isolated follow-up lacks its exact original Map duty')
                    repairs[assigned[0]] = issue
    reason = ('seed-incomplete' if 'short-topics' in phases else
              'required-map-incomplete' if required_map_missing else 'partial')
    require(report['reason'] == reason, 'mapping isolation reason differs from its native stage')
    return reason, repairs


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
