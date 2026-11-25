"""Authorize one fresh conversion attempt for an unfinished native source card."""
from contextlib import closing
import json
import os

from hermes_source_units import FileIngestWorkflowService
from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import completion_connection, require, safe_id
from hermes_source_units.source_units import _exclusive_lock, _load_json, _sha, _vault_path
from hermes_source_units.validation import fingerprint
from incremental_preparation_state import can_retry_preparation, native_state, startup_failure_evidence

KIND = 'preparation-unfinished'
STARTUP_KIND = 'preparation-runtime-startup'
CONTRACT = 'incremental-preparation-retry/v1'
PAUSE_PROJECTION = 'Vault gate or outside active dispatch scope'


def retry(domain, request):
    from incremental_ingest_controller import resume_run

    if request.get('retry_kind') == STARTUP_KIND:
        return authorize_startup_retry(domain, request)
    result = authorize_retry(domain, request)
    resume_run(domain, request['run_id'])
    return result


def authorize_startup_retry(domain, request):
    """Stage the exact original pre-registration failure under a manual pause."""
    from hermes_cli import kanban_db as kb
    from ingest_kanban import IngestKanbanAdapter
    from prepare_short_evidence_test import save
    from program_worker import canonical_binding

    require(set(request) == {'run_id', 'actor', 'operation_id', 'control_revision', 'run_revision',
                            'source_path', 'source_sha256', 'retry_kind', 'native_task_id', 'native_run_id',
                            'source_workflow_revision'} and request['retry_kind'] == STARTUP_KIND,
            'exact source startup recovery request required')
    require(not any(os.environ.get(key) for key in ('HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_DB')),
            'source preparation recovery requires a trusted operator', 'ACCESS_DENIED')
    run_id, operation_id, task_id = (safe_id(request[key]) for key in ('run_id', 'operation_id', 'native_task_id'))
    require(type(request['native_run_id']) is int and request['native_run_id'] > 0,
            'source startup recovery requires the ended native run ID')
    source_path, digest = request['source_path'], request['source_sha256']
    run = domain.status(run_id)
    workflow_id = safe_id(run['source_workflow_id'])
    root = domain.root(run_id)
    folder = _vault_path(domain.vault, f'_system/ledgers/ingest-workflows/{workflow_id}')
    dispatch_lock = folder.parent / '.dispatch-locks' / (workflow_id + '.lock')
    controller = IncrementalControl(domain.vault, run_id)
    grant_path = root / 'preparation-retries' / (fingerprint(source_path) + '.json')
    receipt_path = grant_path.with_suffix('.dispatch.json')
    archived_report = grant_path.with_suffix('.failure.json')
    with _exclusive_lock(dispatch_lock), _exclusive_lock(controller.root / '.lock'), \
            _exclusive_lock(grant_path.with_suffix('.operation.lock'), timeout=5, marker=False):
        run, head = domain.status(run_id), controller.status()
        require(request['actor'] == run['actor'] == head['actor'], 'preparation recovery actor differs', 'ACCESS_DENIED')
        grant = _load_json(grant_path) if grant_path.is_file() else None
        if grant:
            require(grant['request'] == request, 'one preparation retry is already authorized for this file')
            if receipt_path.is_file():
                return _load_json(receipt_path)
        require(request['control_revision'] == head['revision'] and request['run_revision'] == run['revision'],
                'preparation recovery revision changed', 'REVISION_CONFLICT')
        admission = head.get('manual_source_admission', {}).get(source_path, {})
        require(run['state'] == 'active' and head['state'] == 'paused' and not head['active_scopes']
                and head['boundary']['stage'] == 'manual' and not controller.sources_active()
                and admission.get('state') != 'held' and run['sources'].get(source_path, {}).get('state') == 'pending',
                'source startup recovery requires a drained manual pause and an admitted unfinished source')
        require({'path': source_path, 'sha256': digest} in run['source_manifest']
                and _sha(_vault_path(domain.vault, source_path).read_bytes()) == digest, 'source original changed', 'SOURCE_CHANGED')
        workflow = FileIngestWorkflowService(domain.vault).status(workflow_id)
        require(workflow['revision'] == request['source_workflow_revision'], 'source workflow revision changed', 'REVISION_CONFLICT')
        require(not workflow['cancel_requested'] and workflow['actor'] == run['actor'] and workflow['batch_id'] is None
                and workflow['scope'].get('workflow_mode') == 'incremental_v1' and workflow['scope'].get('incremental_run_id') == run_id
                and source_path in workflow['scope']['source_paths']
                and not any(row['path'] == source_path for row in workflow['source_outcomes']),
                'source recovery cannot reset an existing preparation outcome')
        name = 'source-prepare:' + fingerprint({'path': source_path, 'content_sha256': digest})[:16]
        board = workflow['kanban']['board_id']
        card, = (row for row in workflow['kanban']['task_map'] if row['node'] == name and row['task_id'] == task_id)
        binding_path = folder / 'bindings' / (fingerprint(card['idempotency_key']) + '.json')
        adapter = IngestKanbanAdapter(domain.vault, enable_workers=True)
        binding, _ = canonical_binding(adapter, binding_path)
        attempt = fingerprint({'node': name, 'input_fingerprint': binding['input_fingerprint']})[:16]
        require(not binding_path.with_suffix('.execution.json').exists()
                and not grant_path.with_suffix('.started.json').exists()
                and not (folder / 'source-preparation' / attempt).exists(),
                'source startup recovery requires no registered execution or conversion attempt')
        report_path = folder / 'reports' / ('failed-' + fingerprint(name)[:16] + '.json')
        report = report_path if report_path.is_file() else archived_report
        require(report.is_file() and not (report_path.exists() and archived_report.exists()),
                'source startup recovery requires its original projection failure report')
        require(_load_json(report) == {'node': name, 'task_id': task_id, 'input_fingerprint': binding['input_fingerprint'],
                'idempotency_key': card['idempotency_key'], 'error_code': 'NATIVE_WORKER_STOPPED',
                'reason': PAUSE_PROJECTION, 'source_failed': False, 'state': 'execution_blocked'},
                'source projection failure does not match the drained original execution')
        log = adapter.kanban.task_log(board, task_id)
        with closing(completion_connection(board)) as native, kb.write_txn(native):
            task = kb.get_task(native, task_id)
            wire = json.loads(task.body) if task and task.body else {}
            require(task and task.created_by == 'ingest-workflow' and task.assignee == 'ingest-program'
                    and task.idempotency_key == card['idempotency_key']
                    and wire.get('worker_binding') == str(binding_path) and wire.get('vault') == str(domain.vault)
                    and wire.get('executor') == 'program-v1' and wire.get('kind') == 'source-prepare'
                    and all(wire.get(key) == binding[key] for key in ('workflow_id', 'node', 'input_fingerprint', 'template_hash')),
                    'native preparation task binding changed')
            evidence = startup_failure_evidence(task, kb.list_runs(native, task_id, include_active=True), log)
            require(evidence and evidence['run_id'] == request['native_run_id'],
                    'native task has no exact pre-registration startup failure')
            require(not adapter.kanban.worker_is_active(board, task_id, workflow_id, evidence['worker_pid']),
                    'original source worker process has not exited')
            verify_startup_events(native, task_id, evidence)
            intent = {'contract': CONTRACT, 'request': request, 'board': board, 'binding': binding,
                      'prior_run_id': evidence['run_id'], 'prior_outcome': evidence['outcome'],
                      'startup_evidence': evidence, 'projection_report_sha256': _sha(report.read_bytes())}
            if grant:
                require(intent == grant, 'source startup recovery evidence changed')
            else:
                require(task.status == 'archived', 'source startup recovery requires the archived original card')
                save(grant_path, intent)
            restore_startup_card(kb, native, task, card, intent)
        if report == report_path:
            os.replace(report_path, archived_report)
        result = {'state': 'staged', 'retry_kind': STARTUP_KIND, 'operation_id': operation_id, 'source_path': source_path,
                  'task_id': task_id, 'node': name, 'input_fingerprint': binding['input_fingerprint'],
                  'prior_run_id': evidence['run_id'], 'pause_released': False}
        save(receipt_path, result)
        return result


def verify_startup_events(native, task_id, evidence):
    """Bind the sole failed startup to the native dispatch and pause receipts."""
    rows = native.execute('SELECT kind, payload FROM task_events WHERE task_id=? AND run_id=? ORDER BY id',
                          (task_id, evidence['run_id']))
    events = [(row[0], json.loads(row[1])) for row in rows if row[0] in ('claimed', 'spawned', 'scheduled')]
    claims = [data for kind, data in events if kind == 'claimed']
    starts = [data for kind, data in events if kind == 'spawned']
    require(len(claims) == len(starts) == 1 and claims[0]['lock'] == 'ingest-program-dispatch'
            and claims[0]['run_id'] == evidence['run_id'] and starts[0]['pid'] == evidence['worker_pid'],
            'native claim and spawn receipts do not match the original startup failure')
    if evidence['outcome'] == 'scheduled':
        require([data for kind, data in events if kind == 'scheduled'] == [{'reason': PAUSE_PROJECTION}],
                'native pause receipt does not match the drained original startup failure')


def restore_startup_card(kb, native, task, card, intent):
    """Restore an archived source to a held state without creating a new run."""
    event = {'prior_run_id': intent['prior_run_id'], 'operation_id': intent['request']['operation_id']}
    kind = 'preparation_startup_recovery_staged'
    events = [json.loads(row[0]) for row in native.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND kind=?', (task.id, kind))]
    if task.status == 'archived':
        require(not events, 'original startup recovery card was re-archived after staging')
        changed = native.execute("UPDATE tasks SET status='scheduled' WHERE id=? AND status='archived' "
            "AND body=? AND idempotency_key=? AND claim_lock IS NULL AND current_run_id IS NULL AND worker_pid IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM tasks WHERE idempotency_key=? AND id<>? AND status<>'archived')",
            (task.id, task.body, card['idempotency_key'], card['idempotency_key'], task.id))
        require(changed.rowcount == 1, 'native card changed during startup recovery')
        kb._append_event(native, task.id, kind, event)
    else:
        require(task.status == 'scheduled' and events == [event],
                'original startup recovery card left its audited held state')


def startup_retry_receipt(vault, workflow, task_id, node, input_fingerprint):
    """Read the committed retry receipt for this exact task and input, if any."""
    if workflow['scope'].get('workflow_mode') != 'incremental_v1' or not node.startswith('source-prepare:'):
        return None
    run_id = safe_id(workflow['scope']['incremental_run_id'])
    root = _vault_path(vault, f'_system/ledgers/incremental-ingest/{run_id}/preparation-retries')
    for path in root.glob('*.dispatch.json'):
        receipt = _load_json(path)
        if (receipt.get('state') == 'staged' and receipt.get('retry_kind') == STARTUP_KIND
                and receipt.get('task_id') == task_id and receipt.get('node') == node
                and receipt.get('input_fingerprint') == input_fingerprint):
            grant_path = path.with_name(path.name.removesuffix('.dispatch.json') + '.json')
            grant = _load_json(grant_path)
            archived_report = grant_path.with_suffix('.failure.json')
            require(grant['contract'] == CONTRACT and grant['request']['retry_kind'] == STARTUP_KIND
                    and grant['request']['run_id'] == run_id and grant['request']['operation_id'] == receipt['operation_id']
                    and grant['binding']['workflow_id'] == workflow['workflow_id'] and grant['binding']['task_id'] == task_id
                    and grant['binding']['node'] == node and grant['binding']['input_fingerprint'] == input_fingerprint
                    and grant['prior_run_id'] == receipt['prior_run_id'] and archived_report.is_file()
                    and _sha(archived_report.read_bytes()) == grant['projection_report_sha256'],
                    'source startup recovery receipt no longer matches its original evidence')
            return receipt
    return None


def startup_retry_pending(receipt, native_runs):
    """The one new claim is still available unless a later execution was recorded."""
    original = receipt['prior_run_id']
    return (any(row['id'] == original for row in native_runs)
            and all(row['id'] <= original or (row['outcome'] == 'scheduled' and not row.get('worker_pid')
                    and row['started_at'] == row['ended_at']) for row in native_runs))


def authorize_retry(domain, request):
    from hermes_cli import kanban_db as kb
    from ingest_kanban import IngestKanbanAdapter, desired_graph, failure_record
    from prepare_short_evidence_test import save
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
            ended = max(history, key=lambda row: row.id, default=None)
            started = grant_path.with_suffix('.started.json').exists()
            if not grant:
                require(not started and task.status in ('blocked', 'scheduled'), 'native preparation is no longer stopped')
            if task.status in ('blocked', 'scheduled') and not started:
                execution_path = binding_path.with_suffix('.execution.json')
                execution = _load_json(execution_path) if execution_path.is_file() else {}
                require(can_retry_preparation(run_state=run['state'], control_state=head['state'],
                            admission=admission.get('state'), source_state=run['sources'][source_path]['state'],
                            outcome=None, task=native_state(task, ended), execution=execution)
                        and (grant is None or ended.id == grant['prior_run_id']),
                        'native preparation has no evidenced interrupted worker execution')
            if not grant:
                grant = {'contract': CONTRACT, 'request': request, 'board': board, 'binding': binding,
                         'prior_run_id': ended.id, 'prior_outcome': ended.outcome}
                save(grant_path, grant)
            require(grant['board'] == board and grant['binding'] == binding, 'preparation recovery binding changed')
            require(controller.status()['state'] == 'active', 'source recovery cannot release a pause')
            if task.status in ('blocked', 'scheduled') and not started:
                require(kb.unblock_task(native, task.id), 'native preparation could not be released')
            else:
                require(started or task.status in ('ready', 'running'),
                        'preparation recovery is no longer dispatchable')
        result = {'state': 'authorized', 'retry_kind': KIND, 'operation_id': operation_id,
                  'source_path': source_path, 'task_id': task.id, 'prior_run_id': grant['prior_run_id'],
                  'pause_released': False}
        save(grant_path.with_suffix('.dispatch.json'), result)
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
    from prepare_short_evidence_test import save

    save(path.with_suffix('.started.json'), {'operation_id': request['operation_id'], 'run_id': native_run,
                                            'task_id': bound['task_id']})
    return request['operation_id']
