"""Authorize one fresh conversion attempt for an unfinished native source card."""
from contextlib import closing
import json
import os

from hermes_source_units import FileIngestWorkflowService
from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import completion_connection, require, safe_id
from hermes_source_units.source_units import _exclusive_lock, _load_json, _sha, _vault_path
from hermes_source_units.validation import fingerprint
from prepare_short_evidence_test import save

KIND = 'preparation-unfinished'
CONTRACT = 'incremental-preparation-retry/v1'


def retry(domain, request):
    from hermes_cli import kanban_db as kb
    from ingest_kanban import IngestKanbanAdapter, desired_graph, failure_record
    from program_worker import canonical_binding

    require(set(request) == {'run_id', 'actor', 'operation_id', 'control_revision', 'run_revision',
                            'source_path', 'source_sha256', 'retry_kind'} and request['retry_kind'] == KIND,
            'exact source preparation retry request required')
    require(not any(os.environ.get(key) for key in ('HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_DB')),
            'source preparation recovery requires a trusted operator', 'ACCESS_DENIED')
    run_id, operation_id = (safe_id(request[key]) for key in ('run_id', 'operation_id'))
    source_path, digest = request['source_path'], request['source_sha256']
    root = domain.root(run_id)
    controller = IncrementalControl(domain.vault, run_id)
    grant_path = root / 'preparation-retries' / (fingerprint(source_path) + '.json')
    with _exclusive_lock(grant_path.with_suffix('.operation.lock'), timeout=5, marker=False):
        run, head = domain.status(run_id), controller.status()
        require(request['actor'] == run['actor'] == head['actor'], 'preparation recovery actor differs', 'ACCESS_DENIED')
        grant = _load_json(grant_path) if grant_path.is_file() else None
        if grant:
            require(grant['request'] == request, 'one preparation retry is already authorized for this file')
            if grant_path.with_suffix('.dispatch.json').is_file():
                return _load_json(grant_path.with_suffix('.dispatch.json'))
        else:
            require(request['control_revision'] == head['revision'] and request['run_revision'] == run['revision'],
                    'preparation recovery revision changed', 'REVISION_CONFLICT')
        admission = head.get('manual_source_admission', {}).get(source_path, {})
        require(run['state'] == 'active' and head['state'] == 'active' and admission.get('state') != 'held'
                and run['sources'].get(source_path, {}).get('state') == 'pending',
                'only an unfinished admitted source in an active run can retry')
        require(any(row['path'] == source_path and row['sha256'] == digest for row in run['source_manifest'])
                and _sha(_vault_path(domain.vault, source_path).read_bytes()) == digest, 'source original changed', 'SOURCE_CHANGED')
        workflow = FileIngestWorkflowService(domain.vault).status(run['source_workflow_id'])
        require(not workflow['cancel_requested'] and workflow['actor'] == run['actor']
                and workflow['batch_id'] is None and workflow['scope'].get('incremental_run_id') == run_id
                and not any(row['path'] == source_path for row in workflow['source_outcomes']),
                'source recovery cannot reset an existing preparation outcome')
        name = 'source-prepare:' + fingerprint({'path': source_path, 'content_sha256': digest})[:16]
        board = workflow['kanban']['board_id']
        card, = (row for row in workflow['kanban']['task_map'] if row['node'] == name)
        folder = domain.vault / '_system/ledgers/ingest-workflows' / workflow['workflow_id']
        binding_path = folder / 'bindings' / (fingerprint(card['idempotency_key']) + '.json')
        adapter = IngestKanbanAdapter(domain.vault, enable_workers=True)
        binding, _ = canonical_binding(adapter, binding_path)
        adapter.worker_check(binding)
        graph = desired_graph(adapter.workflow, workflow)
        require(name in adapter._source_window(workflow, graph)
                and failure_record(folder / 'reports' / ('failed-' + fingerprint(name)[:16] + '.json')) is None,
                'native preparation is outside capacity or requires a reviewed correction')
        with closing(completion_connection(board)) as native:
            task = kb.get_task(native, card['task_id'])
            wire = json.loads(task.body) if task and task.body else {}
            require(task and task.created_by == 'ingest-workflow'
                    and wire.get('worker_binding') == str(binding_path)
                    and all(wire.get(key) == binding[key] for key in ('workflow_id', 'node', 'input_fingerprint')),
                    'native preparation task binding changed')
            history = kb.list_runs(native, task.id, include_active=False)
            if not grant:
                ended = max(history, key=lambda row: row.id, default=None)
                require(task.status in ('blocked', 'scheduled') and not task.claim_lock and not task.worker_pid
                        and ended and ended.ended_at is not None and ended.outcome != 'completed',
                        'native preparation has no stopped, unfinished attempt')
                grant = {'contract': CONTRACT, 'request': request, 'board': board, 'binding': binding,
                         'prior_run_id': ended.id, 'prior_outcome': ended.outcome}
                save(grant_path, grant)
            require(grant['board'] == board and grant['binding'] == binding, 'preparation recovery binding changed')
            require(controller.status()['state'] == 'active', 'source recovery cannot release a pause')
            if task.status in ('blocked', 'scheduled') and not grant_path.with_suffix('.started.json').exists():
                require(kb.unblock_task(native, task.id), 'native preparation could not be released')
            else:
                require(grant_path.with_suffix('.started.json').exists() or task.status in ('ready', 'running'),
                        'preparation recovery is no longer dispatchable')
        result = {'state': 'authorized', 'retry_kind': KIND, 'operation_id': operation_id,
                  'source_path': source_path, 'task_id': task.id, 'prior_run_id': grant['prior_run_id'],
                  'pause_released': False}
        save(grant_path.with_suffix('.dispatch.json'), result)
    from incremental_ingest_controller import attach_sources, spawn

    attach_sources(domain, run_id)
    spawn(domain.vault, run_id)
    return result


def conversion_generation(adapter, bound, source):
    """Start fresh output under one authorised native run and preserve the prior attempt."""
    workflow = adapter.workflow.status(bound['workflow_id'])
    run_id = workflow['scope'].get('incremental_run_id')
    if not run_id:
        return ''
    root = adapter.workflow.vault / '_system/ledgers/incremental-ingest' / safe_id(run_id)
    path = root / 'preparation-retries' / (fingerprint(source['path']) + '.json')
    if not path.is_file():
        return ''
    grant = _load_json(path)
    request = grant['request']
    native_run = int(os.environ.get('HERMES_KANBAN_RUN_ID', '0'))
    require(grant['contract'] == CONTRACT and request['source_sha256'] == source['content_sha256']
            and request['source_path'] == source['path'] and request['run_id'] == run_id
            and all(bound.get(key) == grant['binding'][key] for key in ('workflow_id', 'node', 'task_id', 'template_hash'))
            and os.environ.get('HERMES_KANBAN_TASK') == bound['task_id']
            and os.environ.get('HERMES_KANBAN_BOARD') == grant['board'] and native_run > grant['prior_run_id'],
            'source preparation retry escaped its native task or input')
    save(path.with_suffix('.started.json'), {'operation_id': request['operation_id'], 'run_id': native_run,
                                            'task_id': bound['task_id']})
    return request['operation_id']
