"""Read-only scope projections for source pipelines and shared delivery rounds."""
from collections import Counter
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import sqlite3

STAGES = (('prepare', '来源准备'), ('mapping', '证据映射'), ('coordination', '页面协调'),
          ('writing', '页面写作'), ('formal-build', '正式构建'), ('release', '发布与索引'),
          ('acceptance', '最终验收'), ('closure', '运行收尾'))


def read(path):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}


def native_tasks(board):
    """Read an existing board only; unavailable native access is not completion."""
    if not board or os.name != 'posix':
        return {}
    try:
        from hermes_cli import kanban_db as kb, kanban_db_connect as kc
        if board not in {row['slug'] for row in kb.list_boards()}:
            return {}
        with closing(kc.connect(board=board)) as connection:
            result = {}
            for task in kb.list_tasks(connection, include_archived=True):
                runs = kb.list_runs(connection, task.id, include_active=False)
                last = max(runs, key=lambda row: row.id, default=None)
                result[task.id] = {'task_id': task.id, 'status': task.status,
                                   'claimed': bool(task.claim_lock),
                                   'last_outcome': getattr(last, 'outcome', None),
                                   'created_by': getattr(task, 'created_by', None), 'body': getattr(task, 'body', None)}
            return result
    except (ImportError, OSError, ValueError, RuntimeError, sqlite3.Error):
        return {}


def stage(key, state='pending', **info):
    return {'key': key, 'name': dict(STAGES)[key], 'state': state, **info}


def aggregate(states):
    if not states:
        return 'pending'
    if 'running' in states:
        return 'running'
    for value in ('pausing', 'paused'):
        if value in states:
            return value
    if all(value == 'completed' for value in states):
        return 'completed'
    if 'failed' in states:
        return 'failed' if all(value == 'failed' for value in states) else 'partial'
    if 'held' in states or 'partial' in states:
        return 'partial' if any(value in ('completed', 'partial') for value in states) else 'held'
    return 'active' if 'completed' in states or 'active' in states else 'pending'


def apply_boundary(value, control, kind, identity):
    boundary = control.get('boundary') or {}
    scope = boundary.get('scope') or {}
    if (control.get('state') in ('paused', 'pausing') and boundary.get('stage') == value['key']
            and scope.get('scope_kind') == kind and scope.get('scope_id') == identity):
        value.update(result_state=value['state'], state=control['state'], boundary=True)
    return value


def active_scope(control, kind, identity, phase):
    return any(row.get('scope_kind') == kind and row.get('scope_id') == identity and row.get('stage') == phase
               for row in control.get('active_scopes', {}).values())


def operation_state(directory, control, kind, identity, phase, *, complete=False, native_reader=None):
    if complete:
        return 'completed'
    operation = read(directory / 'operation.json')
    tasks = (native_reader or native_tasks)(operation.get('board')).values() if operation else []
    if any(row['status'] == 'running' or row['claimed'] for row in tasks):
        return 'running'
    if any(row['status'] in ('failed', 'triage') or
           (row['status'] == 'blocked' and row.get('last_outcome') in ('failed', 'error')) for row in tasks):
        return 'failed'
    if active_scope(control, kind, identity, phase):
        return 'running'
    results = [read(path) for path in (directory / 'jobs').glob('*.result.json')]
    if any(row.get('state') == 'failed' for row in results):
        return 'failed'
    return 'held' if results and all(row.get('state') == 'settled' for row in results) else 'pending'


def round_projection(root, rid, run, control, *, native_reader=None):
    folder = root / 'rounds' / rid
    input_ = read(folder / 'input.json')
    delivery = read(folder / 'delivery.json')
    settlement = read(folder / 'settlement.json')
    changes = read(folder / 'page-changes.json')
    changes = changes if isinstance(changes, list) else []
    writing = read(folder / 'writing-disposition.json')
    native = root / 'native/round' / rid
    writer = native / 'writing'
    base_writer = writer
    amendment = control.get('writing_amendments', {}).get(rid)
    repair = amendment or control.get('writing_continuations', {}).get(rid) or control.get('writing_repairs', {}).get(rid)
    if repair:
        writing = read(folder / 'writing-amendments' / amendment['operation_id'] / 'disposition.json'
                       if amendment else folder / 'writing-revision.json')
        writer = writer / 'recoveries' / repair['operation_id']
        original_repair = control.get('writing_continuations', {}).get(rid) or control.get('writing_repairs', {}).get(rid)
        if original_repair:
            base_writer = base_writer / 'recoveries' / original_repair['operation_id']
    shared = {'scope_kind': 'round', 'scope_id': rid, 'shared': True}
    phases = []
    coordination = operation_state(native / 'coordination', control, 'round', rid,
                                   'coordination', complete=(folder / 'page-changes.json').is_file(), native_reader=native_reader)
    gaps = read(folder / 'coordination-gaps.json') or []
    if gaps or any(row.get('action') == 'hold' for row in changes):
        coordination = 'partial' if any(row.get('action') != 'hold' for row in changes) else 'held'
    phases.append(stage('coordination', coordination, **shared))
    write_state = operation_state(writer, control, 'round', rid, 'writing', complete=bool(writing), native_reader=native_reader)
    if writing.get('held'):
        write_state = 'partial' if writing.get('written') else 'held'
    phases.append(stage('writing', write_state, **shared))
    prefix = delivery.get('state')
    built = prefix in ('build_committed', 'release_applying', 'release_applied', 'index_pending', 'index_verified', 'committed')
    phases.append(stage('formal-build', 'completed' if built else 'running' if prefix == 'build_committing' else
                        operation_state(native / 'formal-build', control, 'round', rid, 'formal-build', native_reader=native_reader), **shared))
    phases.append(stage('release', 'completed' if prefix in ('index_verified', 'committed') else
                        'running' if prefix in ('release_applying', 'release_applied', 'index_pending') else 'pending', **shared))
    phases.append(stage('acceptance', 'completed' if prefix == 'committed' else 'pending', **shared))
    if settlement.get('state') == 'held':
        for value in phases:
            if value['state'] == 'pending':
                value.update(state='held', detail=settlement.get('reason', '本轮保留未完成范围'))
    for value in phases:
        apply_boundary(value, control, 'round', rid)
    return {'round_id': rid, 'active': rid == run.get('active_round_id'), 'mapping_count': len(input_.get('map_result_ids', [])),
            'page_count': len(changes), 'delivery_state': prefix or settlement.get('state'), 'release_id': delivery.get('release_id'),
            'snapshots': input_.get('snapshots', []), 'map_result_ids': input_.get('map_result_ids', []),
            'page_changes': changes, 'stages': phases, 'coordination_gaps': gaps,
            'writing_held': writing.get('held', []), 'build_run_id': delivery.get('build_run_id'),
            'provider_generation': (delivery.get('provider') or {}).get('index_generation'),
            'writing_workspace': base_writer.relative_to(root).as_posix(),
            **({'original_pages_written': writing['original_pages'],
                'independent_section_candidates': len(writing['independent_sections'])}
               if amendment and 'original_pages' in writing else {})}


def source_pipeline(item, root, run, control, source_workflow, rounds, *, native_reader=None):
    from incremental import fingerprint
    path = item['path']
    manifest = next((row for row in run.get('source_manifest', []) if row['path'] == path), {})
    digest = manifest.get('sha256') or (item.get('admission') or {}).get('sha256')
    node = 'source-prepare:' + fingerprint({'path': path, 'content_sha256': digest})[:16]
    card = next((row for row in source_workflow.get('kanban', {}).get('task_map', []) if row.get('node') == node), {})
    task = (native_reader or native_tasks)(source_workflow.get('kanban', {}).get('board_id')).get(card.get('task_id'), {})
    outcome = next((row for row in source_workflow.get('source_outcomes', []) if row.get('path') == path), {})
    reason = outcome.get('reason') or outcome.get('error_code') or item.get('reason') or item.get('error')
    prepared = bool(item.get('snapshot_id'))
    prep_state = ('completed' if prepared else 'failed' if item['state'] == 'failed' or outcome.get('status') == 'failed'
                  else 'running' if task.get('status') == 'running' or task.get('claimed')
                  else 'held' if task.get('status') in ('blocked', 'scheduled', 'archived') and task.get('last_outcome')
                  else 'partial' if outcome.get('status') == 'ready' else 'pending')
    pipeline = [stage('prepare', prep_state, detail=reason, task_id=card.get('task_id'), native_status=task.get('status'),
                      error_code=outcome.get('error_code'), evidence_refs=outcome.get('artifact_refs', []))]
    snapshot = item.get('snapshot_id')
    mapping = 'completed' if item['state'] == 'mapped' else 'held' if prep_state == 'failed' else (
        operation_state(root / 'native/source' / snapshot / 'mapping', control, 'source', snapshot, 'mapping', native_reader=native_reader) if snapshot else 'pending')
    pipeline.append(apply_boundary(stage('mapping', mapping), control, 'source', snapshot))
    related = [row for row in rounds if snapshot and snapshot in row['snapshots']]
    resource = None
    pointer = item.get('snapshot') or {}
    if pointer.get('ref'):
        target = (root.parents[3] / pointer['ref']).resolve()
        vault = root.parents[3].resolve()
        if target.is_relative_to(vault):
            resource = read(target).get('identity', {}).get('resource_id')
    dependencies, dependent_changes = {}, {}
    for row in related:
        matching = [change for change in row['page_changes']
            if snapshot in change.get('coverage_scope', {}).get('snapshots', []) or
               (resource and any(ref.get('unit_ref', {}).get('resource_id') == resource
                for ref in [*change.get('retained_support_refs', []), *change.get('added_support_refs', [])]))]
        dependencies[row['round_id']] = list(dict.fromkeys(change['page_id'] for change in matching))
        dependent_changes[row['round_id']] = [change['change_id'] for change in matching if 'change_id' in change]
    for key, _ in STAGES[2:-1]:
        states = [next(value for value in row['stages'] if value['key'] == key)['state'] for row in related]
        value = aggregate(states)
        if not related and prep_state == 'failed':
            value = 'held'
        pipeline.append(stage(key, value, shared=True, round_ids=[row['round_id'] for row in related],
                              detail='共享轮次结果' if related else '等待本来源映射进入共享轮次'))
    closed = run.get('state') in ('completed', 'partial')
    pipeline.append(apply_boundary(stage('closure', run.get('state') if closed else 'pending', shared=True,
                                       detail='整次运行统一收尾'), control, 'run', run.get('run_id')))
    retry = retry_capability(item, root, run, control, native_reader=native_reader)
    if not retry['allowed'] and prep_state == 'failed':
        retry['reason'] = reason or '此来源失败尚无可用的受控重试入口。'
    display = mapping if item['state'] == 'prepared' and mapping in ('running', 'failed', 'held', 'paused', 'pausing') else prep_state if item['state'] == 'pending' else item['state']
    item.update(source_sha256=digest, display_state=display, pipeline=pipeline, native_preparation={k: v for k, v in task.items() if k != 'body'}, failure_reason=reason, related_rounds=[
        {'round_id': row['round_id'], 'dependent_page_ids': dependencies[row['round_id']], 'shared': True,
         'dependent_change_ids': dependent_changes[row['round_id']],
         'role': 'input' if item.get('map_result_id') in row['map_result_ids'] else 'context'} for row in related], retry=retry)
    return item


def retry_capability(item, root, run, control, *, native_reader=None):
    """Advertise only a stopped, exactly bound, never-consumed mapping request."""
    from incremental import fingerprint

    unavailable = {'allowed': False, 'reason': '当前没有可安全重试的未消耗请求。'}
    snapshot = item.get('snapshot_id')
    if item['state'] != 'prepared' or not snapshot or run.get('state') != 'active' or control.get('state') == 'cancelled':
        return unavailable
    directory = root / 'native/source' / snapshot / 'mapping'
    operation_path = directory / 'operation.json'
    operation = read(operation_path)
    scope = operation.get('incremental', {})
    if (operation.get('workflow_mode') != 'incremental_v1' or scope.get('run_id') != run.get('run_id', root.name)
            or scope.get('scope_kind') != 'source' or scope.get('scope_id') != snapshot or scope.get('stage') != 'mapping'):
        return unavailable
    tasks = list((native_reader or native_tasks)(operation.get('board')).values())
    if not tasks or any(row['claimed'] or row['status'] in ('running', 'ready', 'todo', 'triage') for row in tasks):
        return {**unavailable, 'reason': '原生任务尚未停止，暂不能重试。'}
    matched = []
    reserved = {row['request_fingerprint'] for row in run.get('reservations', {}).values()}
    for job in (directory / 'jobs').glob('*.json'):
        value = read(job)
        if value.get('request', {}).get('id') != job.stem or job.with_suffix('.result.json').exists():
            continue
        expected = {'contract': operation['contract'], 'operation_sha256': hashlib.sha256(operation_path.read_bytes()).hexdigest(),
                    'job': job.name, 'job_sha256': hashlib.sha256(job.read_bytes()).hexdigest()}
        bound = []
        for task in tasks:
            try:
                body = json.loads(task.get('body') or '')
            except (ValueError, TypeError):
                continue
            if body == expected and task.get('created_by') == operation['contract']:
                bound.append(task)
        if len(bound) != 1 or bound[0]['status'] != 'blocked':
            return unavailable
        if (any(job.parent.glob(job.stem + '.attempt-*.json')) or any(job.parent.glob(job.stem + '.audit-*.json'))
                or any(fingerprint({'request': job.stem, 'ordinal': n}) in reserved for n in (1, 2))):
            return {**unavailable, 'reason': '请求已产生调用记录，需要按已有凭据恢复。'}
        matched.append(job.stem)
    if not matched:
        return unavailable
    return {'allowed': True, 'reason': '仅恢复已停止且未消耗的原生映射请求，保留已完成成果。',
            'retry_kind': 'mapping-unconsumed', 'request_ids': matched}


def overall_stages(sources, rounds, run, control):
    result = []
    final_inputs = all(row['state'] in ('mapped', 'failed') for row in sources) and not run.get('active_round_id') and set(
        run.get('map_result_ids', [])) == set(run.get('consumed_map_ids', []))
    for key, _ in STAGES:
        values = ([next(row for row in source['pipeline'] if row['key'] == key)['state'] for source in sources]
                  if key in ('prepare', 'mapping') else [next(row for row in round_['stages'] if row['key'] == key)['state']
                      for round_ in rounds] if key != 'closure' else [run['state'] if run.get('state') in ('completed', 'partial') else 'pending'])
        state = aggregate(values)
        if key not in ('prepare', 'mapping', 'closure') and state == 'completed' and not final_inputs:
            state = 'active'
        value = stage(key, state, counts=dict(Counter(values)), scope='source' if key in ('prepare', 'mapping') else 'round' if key != 'closure' else 'run')
        boundary = control.get('boundary') or {}
        if control.get('state') in ('paused', 'pausing') and boundary.get('stage') == key:
            value.update(result_state=state, state=control['state'], boundary=True)
        result.append(value)
    return result
