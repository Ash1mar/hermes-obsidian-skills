"""Review and authorize one original Map request through its appropriate recovery path."""
from contextlib import closing
import os
import re

from hermes_source_units.incremental_control import (IncrementalControl, SUPPLEMENT_AUTOMATION,
    SUPPLEMENT_AUTOMATION_BOUNDARIES, SUPPLEMENT_AUTOMATION_CONTRACT, SUPPLEMENT_AUTOMATIC_STAGES)
from hermes_source_units.incremental_sources import completion_connection, pinned, read_pin, require as enforce, safe_id
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import service
from short_evidence_pass import require


PREVIEW_FIELDS = {'run_id', 'actor', 'operation_id', 'run_revision', 'control_revision', 'source_path', 'source_sha256'}
CONFIRM_FIELDS = {'strategy', 'request_id', 'expected_report_sha256'}
AUTOMATION_REQUEST = {'run_id', 'actor', 'operation_id', 'expected_revision', 'expected_run_revision',
                      'boundary_fingerprint', 'execution_scope'}
STRATEGIES = {
    'original': '未封存的任务可续跑原卡',
    'supplemental': '已封存的任务通过独立补跑恢复',
}
HEX = re.compile(r'[0-9a-f]{64}')


def review(domain, request):
    from incremental_map_supplement import supplement
    from incremental_source_retry import recovery_inventory

    run_id, source_path = request['run_id'], request['source_path']
    run = domain.status(run_id)
    control = IncrementalControl(domain.vault, run_id)
    head = control.status()
    require(request['actor'] == run['actor'] == head['actor'], 'Map recovery actor changed')
    sources = [row for row in run['source_manifest'] if row['path'] == source_path]
    require(len(sources) == 1 and sources[0]['sha256'] == request['source_sha256'], 'original source identity changed')
    # A read-only preview can follow a dashboard that was drawn before the
    # controller's latest revision. Confirmation is bound to this fresh review.
    bound = {**request, 'run_revision': run['revision'], 'control_revision': head['revision']}
    try:
        evidence = recovery_inventory(domain, run_id, source_path)
    except (ContractError, OSError) as error:
        unavailable = {'mode': 'unavailable', 'reason': str(error)}
        evidence = {**unavailable, 'evidence_fingerprint': fingerprint(unavailable)}
    mode = evidence['mode']
    outcomes, candidates = [], {}
    automation = None
    for group, disposition in (('eligible', 'eligible'), ('completed', 'reuse'), ('isolated', 'isolated')):
        for row in evidence.get(group, []):
            item = {key: row[key] for key in ('request_id', 'task_id', 'reason', 'reason_code', 'phase') if key in row}
            item['disposition'] = disposition
            outcomes.append(item)
            if group != 'eligible' or mode not in STRATEGIES:
                continue
            request_id = row['request_id']
            selected = {**bound, 'request_ids': [request_id]}
            try:
                if mode == 'original':
                    selected.pop('execution_scope', None)
                    exact = recovery_inventory(domain, run_id, source_path, request_ids=[request_id])
                    require(exact['mode'] == mode and any(r['request_id'] == request_id for r in exact['eligible']),
                            'original Map request changed while reviewing')
                    selected.update(retry_kind='mapping-unconsumed', evidence_fingerprint=exact['evidence_fingerprint'])
                else:
                    plan = supplement(domain.vault, selected, preview=True)
                    selected['expected_report_sha256'] = plan['report_sha256']
                    item['supplement_run_id'] = plan.get('supplement_run_id')
                    if request.get('execution_scope') == SUPPLEMENT_AUTOMATION:
                        automation = {'execution_scope': SUPPLEMENT_AUTOMATION,
                                      'authorized_stages': plan['authorized_stages'],
                                      'release_baseline': plan['release_baseline']}
                candidates[request_id] = selected
            except (ContractError, ValueError, OSError) as error:
                item.update(disposition='isolated', reason=str(error))
    stable = (domain.status(run_id)['revision'] == bound['run_revision']
              and control.status()['revision'] == bound['control_revision'])
    if not stable:
        candidates.clear()
    reason = ('运行状态已推进，请重新预览' if not stable else evidence.get('reason') or '此来源没有符合条件的请求')
    strategies = [{'id': key, 'allowed': key == mode and bool(candidates),
                   'reason': label if key == mode and candidates else reason,
                   'request_ids': sorted(candidates) if key == mode else []} for key, label in STRATEGIES.items()]
    if automation and candidates:
        next(row for row in strategies if row['id'] == 'supplemental').update(automation)
    document = {'request': bound, 'strategies': strategies, 'outcomes': outcomes, 'mode': mode,
                'review_status': 'ready' if candidates else 'unavailable',
                'evidence_fingerprint': evidence['evidence_fingerprint'],
                'execution_intents': {key: fingerprint(payload) for key, payload in candidates.items()}}
    return {**document, 'report_sha256': fingerprint(document)}, candidates


def recover(vault, request, *, preview=False):
    """Review any run state, then revalidate and durably authorize one card."""
    from incremental_map_supplement import supplement
    from incremental_source_retry import retry

    optional = {'execution_scope'} & request.keys()
    fields = PREVIEW_FIELDS | optional
    require(set(request) == fields | (set() if preview else CONFIRM_FIELDS)
            and (not optional or request['execution_scope'] == SUPPLEMENT_AUTOMATION),
            'Map recovery requires one exact operator request')
    run_id, operation_id = safe_id(request['run_id']), safe_id(request['operation_id'])
    domain = service(vault, run_id)
    if preview:
        return review(domain, request)[0]

    strategy, request_id = request['strategy'], request['request_id']
    require(strategy in STRATEGIES and isinstance(request_id, str) and HEX.fullmatch(request_id),
            'Map recovery requires one selected original request')
    root = _vault_path(domain.vault, f'_system/ledgers/incremental-map-recoveries/{run_id}')
    path = root / (operation_id + '.json')
    with _exclusive_lock(root / ('.' + operation_id + '.lock'), timeout=0, marker=False):
        if path.exists():
            saved = _load_json(path)
            require(saved['request'] == request, 'Map recovery operation identifies different inputs')
        else:
            base = {key: request[key] for key in fields}
            report, candidates = review(domain, base)
            require(report['request'] == base and report['report_sha256'] == request['expected_report_sha256'],
                    'Map recovery preview changed. Review the current evidence.')
            require(report['mode'] == strategy and request_id in candidates,
                    'selected Map request no longer qualifies for this strategy')
            saved = {'contract': 'incremental-map-operator-recovery/v1', 'request': request,
                     'execution_intent': candidates[request_id], 'state': 'prepared'}
            _write_atomic(path, _json_bytes(saved))

        if saved['state'] == 'committed':
            receipt = saved['receipt']
        else:
            selected = saved['execution_intent']
            result = (retry(domain.vault, selected) if strategy == 'original'
                      else supplement(domain.vault, selected, preview=False))
            receipt = {**result, 'strategy': strategy, 'request_id': request_id, 'source_path': request['source_path']}
            _write_atomic(path, _json_bytes({**saved, 'state': 'committed', 'receipt': receipt}))
    if strategy == 'supplemental' and receipt.get('execution_scope') == SUPPLEMENT_AUTOMATION:
        from incremental_ingest_controller import resume_run

        resume_run(domain, receipt['supplement_run_id'])
    return receipt


def drained_boards(domain, run_id):
    """Prove that this supplement owns no active native work at authorization."""
    import run_short_evidence_test as native
    from native_stop_inventory import workflow_processes

    kb, _, _ = native.runtime()
    boards = set()
    for path in (domain.root(run_id) / 'native').rglob('operation.json'):
        operation = _load_json(path)
        require(operation.get('workflow_mode') == 'incremental_v1'
                and operation['incremental']['run_id'] == run_id, 'automation native board escaped this supplement')
        boards.add(operation['board'])
    for board in boards:
        enforce(not workflow_processes('', board), 'supplement native worker is still active', 'NATIVE_ACTIVE')
        with closing(completion_connection(board)) as connection:
            tasks = kb.list_tasks(connection)
        enforce(not any(task.claim_lock or task.status in ('ready', 'running', 'todo', 'triage') for task in tasks),
                'supplement native board is still active', 'NATIVE_ACTIVE')
    return sorted(boards)


def automation_report(domain, control, request):
    from pathlib import Path

    from incremental_ingest_controller import process_start
    from incremental_map_supplement import downstream_boundary, release_baseline
    from run_short_evidence_test import sha

    run_id = request['run_id']
    root = domain.root(run_id)
    run, head = domain.status(run_id), control.status()
    boundary = head.get('boundary') or {}
    stage = boundary.get('stage')
    enforce(run['state'] == 'active' and run.get('map_supplement') and head['state'] == 'paused'
            and request['actor'] == run['actor'] == head['actor'] and not head['active_scopes']
            and not head.get('supplement_automation') and stage in SUPPLEMENT_AUTOMATION_BOUNDARIES
            and stage in head['pause_after'] and request['expected_revision'] == head['revision']
            and request['expected_run_revision'] == run['revision']
            and request['boundary_fingerprint'] == fingerprint(boundary),
            'automation needs the exact drained planned supplement checkpoint', 'STALE_INPUT')
    controller = _load_json(root / 'controller.json') if (root / 'controller.json').is_file() else {}
    enforce(not controller.get('start') or controller['start'] != process_start(controller['pid']),
            'supplement controller is still running', 'NATIVE_ACTIVE')
    lineage, _ = domain.map_supplement_lineage(run)
    require(lineage['request'].get('execution_scope') is None, 'supplement already authorized for automatic execution')
    mapping = downstream_boundary(domain, run)
    enforce(mapping and (boundary == mapping if stage == 'mapping' else fingerprint(mapping) in head['released']),
            'supplement mapping checkpoint no longer matches its settled Map', 'STALE_INPUT')
    map_result = domain.read_map(run_id, mapping['evidence']['map_result_id'])
    current = release_baseline(domain.vault)
    enforce(current == lineage['release_baseline'], 'supplement publication baseline changed', 'STALE_INPUT')
    provider = lineage['provider_config']
    enforce(sha(Path(provider['path'])) == provider['sha256'], 'supplement Provider configuration changed', 'STALE_INPUT')
    round_proof = None
    if stage in ('coordination', 'writing'):
        round_id = safe_id(run['active_round_id'])
        enforce(boundary['scope'] == {'scope_kind': 'round', 'scope_id': round_id}
                and domain.round_input(run_id, round_id)['map_result_ids'] == [map_result['map_result_id']],
                'automation round differs from the settled supplement Map', 'STALE_INPUT')
        disposition = 'page-changes.json' if stage == 'coordination' else 'writing-disposition.json'
        evidence_key = 'changes_sha256' if stage == 'coordination' else 'writing_disposition_sha256'
        round_root = root / 'rounds' / round_id
        disposition_path = round_root / disposition
        if stage == 'writing':
            if amendment := head.get('writing_amendments', {}).get(round_id):
                from incremental_writing_amendment import disposition_path as amended_disposition

                disposition_path = amended_disposition(domain, run_id, round_id, amendment['operation_id'])
            elif round_id in head.get('writing_repairs', {}):
                disposition_path = round_root / 'writing-revision.json'
        round_proof = pinned(domain.vault, disposition_path.relative_to(domain.vault).as_posix())
        enforce(round_proof['sha256'] == boundary['evidence'][evidence_key], 'supplement pause evidence changed', 'STALE_INPUT')
    boards = drained_boards(domain, run_id)
    enforce(run == domain.status(run_id) and head == control.status(), 'supplement advanced during automation review', 'STALE_INPUT')
    return {'contract': SUPPLEMENT_AUTOMATION_CONTRACT, 'request': request, 'map_supplement': run['map_supplement'],
            'map_result_id': map_result['map_result_id'], 'round_proof': round_proof, 'release_baseline': current,
            'native_boards': boards,
            'authorized_stages': list(SUPPLEMENT_AUTOMATIC_STAGES[SUPPLEMENT_AUTOMATIC_STAGES.index(stage) + 1:])}


def authorize_supplement(vault, request, *, preview=False):
    """Grant the already completed Map's downstream scope without rewriting its authorization."""
    require(set(request) == AUTOMATION_REQUEST | (set() if preview else {'expected_report_sha256'})
            and request['execution_scope'] == SUPPLEMENT_AUTOMATION, 'exact supplemental automation request required')
    run_id, operation = safe_id(request['run_id']), safe_id(request['operation_id'])
    domain = service(vault, run_id)
    control = IncrementalControl(domain.vault, run_id)
    bound = {key: request[key] for key in AUTOMATION_REQUEST}
    if preview:
        report = automation_report(domain, control, bound)
        return {**report, 'report_sha256': fingerprint(report), 'read_only': True}
    require(not any(os.environ.get(key) for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB',
                                                     'HERMES_DELEGATED_CHILD_CONTEXT')),
            'Map supplement automation requires a trusted operator')
    root = domain.root(run_id)
    path = root / 'map-supplement-automation' / (operation + '.json')

    def apply(report):
        enforce(report['request'] == bound and fingerprint(report) == request['expected_report_sha256'],
                'supplement automation preview changed', 'STALE_INPUT')
        pointer = pinned(domain.vault, path.relative_to(domain.vault).as_posix())
        return control.control({**{key: request[key] for key in ('run_id', 'actor', 'operation_id', 'expected_revision',
                                                               'boundary_fingerprint')},
                                'action': 'continue', 'supplement_automation': pointer})

    if (control.root / 'operations' / (operation + '.json')).is_file():
        report = _load_json(path)
        head = apply(report)
    else:
        with _exclusive_lock(root / '.controller.lock', timeout=0, marker=False):
            report = automation_report(domain, control, bound)
            enforce(fingerprint(report) == request['expected_report_sha256'], 'supplement automation preview changed', 'STALE_INPUT')
            domain._immutable(path, report)
            head = apply(report)
    from incremental_ingest_controller import resume_run

    resume_run(domain, run_id)
    return {'run_id': run_id, 'state': head['state'], 'authorized_stages': report['authorized_stages'],
            'report_sha256': request['expected_report_sha256'], 'control_revision': head['revision']}


def prepare_child_continuation(domain, run, control, request):
    """Bind a child Map checkpoint release to an independently approved publication head."""
    from incremental_map_supplement import check_downstream_release

    extra = {'expected_run_revision', 'expected_release'}
    if not run.get('map_supplement') or request.get('action') != 'continue':
        require(not extra & request.keys(), 'supplement continuation fields require a child Map checkpoint')
        return request

    op = safe_id(request['operation_id'])
    control_root = domain.root(run['run_id']) / 'control'
    path = control_root / 'map-supplement-continuations' / (op + '.json')
    boundary = control.status().get('boundary') or {}
    if not path.is_file() and boundary.get('stage') != 'mapping':
        require(not extra & request.keys(), 'supplement continuation fields require a child Map checkpoint')
        return request

    with _exclusive_lock(path.with_suffix('.lock'), timeout=5, marker=False):
        if path.is_file():
            saved = _load_json(path)
            require(saved['request'] == request, 'child Map continuation operation identifies different inputs')
            if (control_root / 'operations' / path.name).is_file():
                return saved['effective']
        else:
            saved = None
        current = domain.status(run['run_id'])
        head = control.status()
        require(request['actor'] == current['actor'] == head['actor']
                and request.get('expected_run_revision') == current['revision']
                and request['expected_revision'] == head['revision']
                and request.get('boundary_fingerprint') == fingerprint(head.get('boundary')),
                'child Map continuation needs the current run and its exact pause')
        baseline = check_downstream_release(domain, current, head['boundary'],
                                            expected_release=request.get('expected_release'))
        effective = {**request, 'expected_release': baseline}
        if saved is None:
            _write_atomic(path, _json_bytes({'request': request, 'effective': effective}))
        else:
            require(saved['effective'] == effective, 'child Map continuation publication head changed')
        return effective
