"""Console projection for explicit source/round incremental runs."""

from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import sys


MODE = 'incremental_v1'
NETWORK_RECOVERY = 'network_recovery'
PAUSES = ('prepare', 'mapping', 'coordination', 'writing', 'formal-build', 'release', 'acceptance', 'closure')
DEFAULT_BUDGET = {'max_calls': 600, 'max_input_tokens': 12000000, 'max_output_tokens': 3000000,
                  'phase_limits': {'mapping': 360, 'coordination': 30, 'writing': 210}}
DEFAULT_POLICY = {'max_maps_per_round': 1}
SUPPLEMENT_CONTROLS = frozenset({'incremental-continue-round', 'incremental-continue', 'incremental-pause',
                                 'incremental-reconcile', 'incremental-closure-preview', 'incremental-closure-confirm'})


def read(path):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}


def settings(body, config, error):
    if body.get('workflow_mode') not in (None, '', MODE):
        raise error('请选择受支持的处理模式。')
    if body.get('workflow_mode') != MODE:
        return {}
    budget = body['incremental_budget'] if 'incremental_budget' in body else copy.deepcopy(DEFAULT_BUDGET)
    policy = body['incremental_policy'] if 'incremental_policy' in body else dict(DEFAULT_POLICY)
    pause_after = body.get('incremental_pause_after', [])
    manual_sources = body.get('incremental_manual_sources', False)
    if type(manual_sources) is not bool:
        raise error('逐文件手动开始必须为明确的开关。')
    if (not isinstance(budget, dict) or set(budget) != {'max_calls', 'max_input_tokens', 'max_output_tokens', 'phase_limits'}
            or any(type(budget.get(k)) is not int or budget[k] < 1 for k in ('max_calls', 'max_input_tokens', 'max_output_tokens'))
            or not isinstance(budget.get('phase_limits'), dict)
            or set(budget['phase_limits']) != {'mapping', 'coordination', 'writing'}
            or any(type(value) is not int or value < 1 or value > budget['max_calls'] for value in budget['phase_limits'].values())):
        raise error('请填写总调用数、输入/输出 token 上限及三个阶段的调用上限。')
    if not isinstance(policy, dict) or set(policy) != {'max_maps_per_round'} or type(policy['max_maps_per_round']) is not int or policy['max_maps_per_round'] < 1:
        raise error('请填写每轮最多接收的来源映射数。')
    if not isinstance(pause_after, list) or len(pause_after) != len(set(pause_after)) or any(p not in PAUSES for p in pause_after):
        raise error('请选择有效的增量暂停边界。')
    result = {'workflow_mode': MODE, 'incremental_budget': budget, 'incremental_policy': policy,
              'incremental_pause_after': pause_after, 'incremental_provider_config': config['provider_config']}
    if manual_sources:
        result['incremental_manual_sources'] = True
    return result


def root_for(request):
    return Path(request['vault_path']) / '_system/ledgers/incremental-ingest' / request['operation_id']


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def network_continue_binding(run, control):
    boundary = control.get('boundary') or {}
    rid = run.get('active_round_id')
    if (control.get('state') == 'paused' and not control.get('active_scopes')
            and run.get('state') == 'active' and rid and boundary.get('stage') == NETWORK_RECOVERY
            and boundary.get('scope') == {'scope_kind': 'round', 'scope_id': rid}):
        return {'round_id': rid, 'boundary_fingerprint': fingerprint(boundary)}
    return None


def closure_ready(root, run, control, *, interrupted):
    return (run.get('state') == 'active' and not run.get('active_round_id') and not control.get('active_scopes')
            and ((control.get('state') == 'active' and interrupted)
                 or (control.get('state') == 'paused' and (control.get('boundary') or {}).get('stage') == 'closure'))
            and any(row['state'] == 'reserved' for row in run.get('reservations', {}).values())
            and all(row['state'] in ('mapped', 'failed', 'mapping_held') for row in run.get('sources', {}).values())
            and set(run.get('map_result_ids', [])) == set(run.get('consumed_map_ids', []))
            and all((root / 'rounds' / rid / 'settlement.json').is_file() for rid in run.get('round_ids', [])))


def closure_node(directory, root, run, control, rounds, attempts, *, ready, available, supplement_run_id=None):
    node = {'key': 'incremental-closure', 'name': '运行收尾', 'actions': []}
    reserved = {key: row for key, row in run.get('reservations', {}).items() if row['state'] == 'reserved'}
    if run.get('state') in ('completed', 'partial'):
        node['state'] = 'partially-closed' if run['state'] == 'partial' else 'completed'
        if reserved:
            vault = root.parents[3].resolve()
            pointer = control.get('partial_closure') or {}
            path = (vault / pointer.get('ref', '')).resolve()
            report = {}
            if path.is_relative_to(root.resolve()) and path.is_file():
                contents = path.read_bytes()
                if hashlib.sha256(contents).hexdigest() == pointer.get('sha256'):
                    report = json.loads(contents)
            node['detail'] = f'{len(reserved)} 笔用量尚未确认，已保留预算'
            node['reservations'] = {
                key: {'phase': row['phase'], 'disposition': 'retained_unconfirmed', 'actual': None,
                      'reason': report.get('entries', {}).get(key, {}).get('reason', 'unconfirmed'),
                      'evidence': {name: entry for name, entry in report.get('entries', {}).get(key, {}).items()
                                   if name in ('job', 'attempt', 'audit', 'failure', 'native', 'observed_session',
                                               'http_journal', 'http_sends', 'failure_type')}}
                for key, row in reserved.items()}
        return node
    boundary = control.get('boundary') or {}
    at_closure = control.get('state') == 'paused' and boundary.get('stage') == 'closure'
    if not ready:
        return node
    accepted = [row for row in rounds if row['delivery_state'] == 'committed']
    pages = boundary.get('evidence', {}).get('published_pages')
    if pages is None:
        pages = 0
        release = accepted[-1]['release_id'] if accepted else None
        if release and release.isascii() and release.replace('-', '').isalnum():
            pages = len(read(root.parents[3] / '_system/knowledge-releases' / release / 'manifest.json').get('subjects', []))
    node.update(state='closure-accounting',
                detail=f'{len(accepted)} 轮已验收，{pages} 页已发布，{len(reserved)} 笔待对账')
    if not available:
        return node
    latest = attempts[-1] if attempts else {}
    action = {'node': node['key'], 'attempt': latest.get('id'), 'gateway_required': False,
              'control_revision': control['revision'], 'run_revision': run['revision']}
    if supplement_run_id:
        action['supplement_run_id'] = supplement_run_id
    node['actions'].append({**action, 'id': 'incremental-closure-preview', 'label': '核对用量并结案'})
    for attempt in reversed(attempts):
        if (attempt['action'] != 'incremental-closure-preview' or attempt.get('returncode') != 0
                or attempt.get('supplement_run_id') != supplement_run_id):
            continue
        review = read(directory / 'attempts' / attempt['id'] / 'closure-preview.json')
        original = review.get('request', {})
        if ((not supplement_run_id or original.get('run_id') == supplement_run_id)
                and original.get('run_revision') == run['revision']
                and ((at_closure and original.get('expected_revision') == control['revision']
                      and original.get('boundary_fingerprint') == fingerprint(boundary))
                     or (control.get('state') == 'active' and control.get('partial_closure', {}).get('ref', '').endswith(
                         '/' + str(original.get('operation_id')) + '.json')))):
            node.update(reservations=review['reservations'], review_ready=True)
            node['actions'].append({**action, 'id': 'incremental-closure-confirm', 'label': '确认用量处置并结案',
                                    'preview_attempt': attempt['id'], 'report_sha256': review['report_sha256']})
        break
    return node


def execution_view(root, run, control, latest, launcher_active, *, supplement=False):
    controller = read(root / 'controller.json')
    controller_active = None
    if os.name == 'posix' and controller.get('pid'):
        try:
            proc = Path('/proc') / str(int(controller['pid']))
            fields = (proc / 'stat').read_text().split(') ', 1)[1].split()
            controller_active = fields[0] not in ('Z', 'X') and controller.get('start') == fields[19]
        except (FileNotFoundError, ValueError, IndexError):
            controller_active = False
    interrupted = not launcher_active and (controller_active is False or (supplement and controller_active is None))
    waiting_accounting = closure_ready(root, run, control, interrupted=interrupted)
    if run.get('state') in ('completed', 'partial'):
        state = run['state']
    elif waiting_accounting:
        state = 'closure-accounting'
    elif control.get('state') in ('paused', 'pausing', 'cancelled'):
        state = control['state']
    elif latest.get('returncode', 0) and not run:
        state = 'failed'
    elif run and interrupted:
        state = 'interrupted'
    else:
        state = 'running' if run or launcher_active else 'pending'
    return {'root': root, 'run': run, 'control': control, 'controller': controller,
            'controller_active': controller_active, 'waiting_accounting': waiting_accounting, 'state': state}


def supplement_views(root, run, records, latest, active):
    if not records:
        return {}
    from incremental_map_preview import CONFIRM
    from incremental_projection import linked_supplements

    views = {}
    for successor, bound in linked_supplements(root, run, records).items():
        child_started = (latest.get('supplement_run_id') == successor
                         or (latest.get('action') == CONFIRM and latest.get('strategy') == 'supplemental'
                             and latest.get('preview_key') == bound['request']['operation_id']))
        execution = execution_view(bound['root'], bound['run'], bound['control'], latest,
                                   active and child_started, supplement=True)
        views[successor] = {**bound, **execution, 'run_id': successor,
                            'source_path': bound['request']['source_path']}
    return views


def supplement_priority(view):
    return (view['run']['state'] == 'active',
            view['state'] in ('running', 'pausing'),
            view['run']['revision'], view['run_id'])


def control_actions(run, control, controller_active, state, latest, *, available, supplemental_id=None):
    if not available or run.get('state') != 'active' or state == 'closure-accounting':
        return []
    actions = []
    boundary = (control.get('boundary') or {}).get('stage')
    if control.get('state') == 'paused':
        if boundary == NETWORK_RECOVERY:
            binding = network_continue_binding(run, control)
            if binding and controller_active is not True:
                actions.append({'id': 'incremental-continue-round', 'label': '继续本轮', **binding})
        elif not supplemental_id or boundary == 'manual':
            actions.append({'id': 'incremental-continue', 'label': '继续当前作用域'})
    elif control.get('state') == 'active':
        if not control.get('active_scopes') and (not supplemental_id or controller_active is True):
            actions.append({'id': 'incremental-pause', 'label': '暂停增量运行', 'gateway_required': False})
        if not supplemental_id or controller_active is not True:
            actions.append({'id': 'incremental-reconcile', 'label': '接管并核对未完成工作'})
    for action in actions:
        action.update(node='incremental-control', attempt=latest.get('id'), control_revision=control.get('revision'),
                      run_revision=run.get('revision'))
        if supplemental_id:
            action['supplement_run_id'] = supplemental_id
    return actions


def apply_supplement_scope(item, child, closure):
    mapping = next(phase for phase in item['pipeline'] if phase['key'] == 'mapping')
    source = child['run']['sources'][item['path']]
    if mapping['state'] == 'pending' and source['state'] == 'prepared':
        if child['state'] == 'running':
            mapping['state'] = 'running'
        elif child['state'] == 'interrupted':
            mapping['state'] = 'failed'
    result = next((row for row in mapping['map_recovery']['results']
                   if row.get('request_id') == child['request']['request_id']), None)
    if result is None:
        result = {'strategy': 'supplemental', 'request_id': child['request']['request_id']}
        mapping['map_recovery']['results'].append(result)
    result.update(supplement_run_id=child['run_id'], state=child['state'],
                  boundary=(child['control'].get('boundary') or {}).get('stage'),
                  map_result_ids=child['run'].get('map_result_ids', []),
                  reused_receipts=len(child['lineage'].get('completed', [])))
    blocked = [action for action in mapping['actions']
               if action.get('strategy') == 'supplemental' and action.get('request_ids') == [result['request_id']]]
    for action in blocked:
        mapping['actions'].remove(action)
        item['actions'].remove(action)
    if closure and closure.get('state'):
        phase = item['pipeline'][-1]
        phase.update(state=closure['state'], detail=closure.get('detail'))


def progress(directory, request, attempts, active, historical, details, *, legacy_map_retry=False):
    from incremental_map_preview import project_map_recovery, recovery_records

    root = root_for(request)
    run = read(root / 'run.json')
    control = read(root / 'control/head.json')
    latest = attempts[-1] if attempts else {}
    original = execution_view(root, run, control, latest, active)
    map_recoveries = recovery_records(root)
    children = supplement_views(root, run, map_recoveries, latest, active)
    linked = {}
    for child in sorted(children.values(), key=supplement_priority):
        linked[child['source_path']] = child
    successor = max(linked.values(), key=supplement_priority, default=None)
    displayed = successor if successor and original['state'] in ('completed', 'partial') else original
    display_id = displayed.get('run_id')
    state = displayed['state']
    data = {'id': request['operation_id'], 'vault_name': request['vault_name'], 'vault_path': request['vault_path'],
            'workflow_mode': MODE, 'state': state, 'historical': historical, 'launcher_active': active,
            'launcher_state': 'running' if active else 'failed' if latest.get('returncode') else 'completed',
            'source_count': len(request['sources']), 'started_at': attempts[0].get('started_at') if attempts else None}
    if not details:
        return data
    source_workflow = run.get('source_workflow_id', 'ingest-' + request['operation_id'])
    source = read(Path(request['vault_path']) / '_system/ledgers/ingest-workflows' / (source_workflow + '.json'))
    from incremental_projection import native_tasks, overall_stages, round_projection, source_pipeline
    from incremental_node_recovery import source_actions
    native_cache = {}
    def native_reader(board):
        if board not in native_cache:
            native_cache[board] = native_tasks(board)
        return native_cache[board]
    rounds = [round_projection(root, rid, run, control, native_reader=native_reader) for rid in run.get('round_ids', [])]
    rounds_by_id = {row['round_id']: row for row in rounds}
    child_rounds = {child['run_id']: [
        {**round_projection(child['root'], rid, child['run'], child['control'], native_reader=native_reader),
         'supplement_run_id': child['run_id'], 'source_name': Path(child['source_path']).name}
        for rid in child['run'].get('round_ids', [])] for child in linked.values()}
    manual = control.get('manual_source_admission', {})
    sources = [{'name': Path(path).name, 'path': path, **row,
                'admission': manual.get(path), 'actions': []} for path, row in run.get('sources', {}).items()]
    closure = closure_node(directory, displayed['root'], displayed['run'], displayed['control'],
                           child_rounds[display_id] if display_id else rounds, attempts,
                           ready=displayed['waiting_accounting'],
                           available=not historical and not active and displayed['controller_active'] is not True,
                           supplement_run_id=display_id)
    boundary = control.get('boundary') or {}
    network_paused = boundary.get('stage') == NETWORK_RECOVERY and control.get('state') in ('pausing', 'paused')
    actions = control_actions(displayed['run'], displayed['control'], displayed['controller_active'], state, latest,
                              available=not historical and not active, supplemental_id=display_id)
    source_nodes = []
    for index, item in enumerate(sources):
        child = linked.get(item['path'])
        if child:
            prior = item['state']
            sources[index] = item = {'name': item['name'], 'path': item['path'],
                                     **child['run']['sources'][item['path']], 'original_state': prior,
                                     'supplement_run_id': child['run_id'], 'admission': None, 'actions': []}
            source_pipeline(item, child['root'], child['run'], child['control'], source,
                            child_rounds[child['run_id']], native_reader=native_reader)
            item['display_state'] = child['state']
            source_actions(item, child['root'], child['run'], child['control'],
                           {row['round_id']: row for row in child_rounds[child['run_id']]}, latest.get('id'),
                           writable=False, interrupted=child['controller_active'] is not True)
        else:
            source_pipeline(item, root, run, control, source, rounds, native_reader=native_reader)
        admission = item['admission']
        node = 'incremental-source-' + fingerprint({'path': item['path'], 'sha256': item.get('source_sha256')})[:24]
        if (admission and admission.get('state') == 'held' and not historical and not active and not network_paused
                and run.get('state') == 'active' and control.get('state') in ('active', 'paused', 'pausing')):
            item['actions'].append({'id': 'incremental-release-source', 'node': node,
                'label': '开始处理此文件' if control['state'] == 'active' else '允许处理（继续后开始）',
                'attempt': latest.get('id'), 'control_revision': control.get('revision'),
                'run_revision': run.get('revision'), 'source_path': item['path'],
                'source_sha256': admission['sha256']})
        if not child:
            source_actions(item, root, run, control, rounds_by_id, latest.get('id'),
                           writable=not historical and not active and not network_paused and run.get('state') == 'active',
                           interrupted=original['controller_active'] is not True, legacy_map_retry=legacy_map_retry)
        project_map_recovery(directory, root, request, run, control, item, latest, map_recoveries,
                             preview=not historical, confirm=not historical and not active and original['controller_active'] is not True)
        if child:
            apply_supplement_scope(item, child, closure if display_id == child['run_id'] else None)
        if admission or item['actions']:
            source_nodes.append({'key': node, 'name': item['name'], 'state': item['display_state'] if child else item['state'],
                                 'description': '逐文件手动准入；允许处理不会释放暂停边界。',
                                 'actions': item['actions']})
    released = read(Path(request['vault_path']) / '_system/metadata/knowledge-release-state.json').get('current_release_id')
    index = read(Path(request['vault_path']) / '_system/reports/retrieval-index-manifest.json')
    delivery_window = read(Path(request['vault_path']) / '_system/metadata/incremental-delivery-state.json')
    delivering = bool(delivery_window and delivery_window.get('state') not in ('committed', 'aborted'))
    counts = dict(Counter(row['state'] for row in sources))
    projected_rounds = [*rounds, *(row for values in child_rounds.values() for row in values)]
    stages = overall_stages(sources, projected_rounds, displayed['run'], displayed['control'])
    if closure.get('state'):
        stages[-1].update({key: value for key, value in closure.items() if key != 'key'})
        stages[-1].pop('counts', None)
    data.update(nodes=[{'key': 'incremental-control', 'name': '增量运行控制', 'state': state,
                       'description': '暂停作用于整个增量运行。继续只释放当前已核验的边界。', 'actions': actions},
                      *source_nodes, closure],
                incremental={'sources': sources, 'source_counts': counts, 'rounds': projected_rounds,
                             'overall_stages': stages,
                             'active_round_id': displayed['run'].get('active_round_id'), 'control': displayed['control'],
                             'budget': displayed['run'].get('budget'), 'controller': displayed['controller'],
                             'controller_active': displayed['controller_active'], 'delivery_window': delivery_window,
                             **({'original_state': original['state'], 'supplemental_run': {
                                 'run_id': successor['run_id'], 'state': successor['state'],
                                 'boundary': (successor['control'].get('boundary') or {}).get('stage'),
                                 'source_name': Path(successor['source_path']).name}}
                                if successor else {})},
                attempts=attempts, sources=request['sources'], active_node=None,
                workflow_id=source.get('workflow_id'), source_revision=source.get('revision'),
                source_stage=source.get('current_stage'), source_board=source.get('kanban', {}).get('board_id'),
                short_board=None, pause=(displayed['control'].get('boundary') or {}).get('stage'),
                operator={}, source_wait={}, pause_request={}, receipts=len(run.get('map_result_ids', [])) + sum(
                    len(child['run'].get('map_result_ids', [])) for child in linked.values()),
                release_id=released, indexed=bool(not delivering and released and index.get('release_id') == released and index.get('status') == 'ready'),
                query_enabled=request['retrieval_policy']['query_enabled'])
    return data


def entrypoint(request, config):
    script = Path(config['skills_root']) / 'hermes-obsidian-governed-ingest-orchestrator/scripts/manage_incremental_ingest.py'
    if not script.is_file():
        raise ValueError('已安装的 Skill 缺少增量入口。')
    return [sys.executable, '-B', str(script), '--vault', request['vault_path']]


def command(directory, attempt, config, save):
    request = read(directory / 'request.json')
    args = entrypoint(request, config)
    if attempt['action'] == 'start':
        return args + ['start', '--request', str(directory / 'request.json'), '--record', str(directory / 'incremental-start.json'),
                       '--bootstrap-skill', str(Path(config['skills_root']) / 'hermes-obsidian-vault-bootstrap')]
    from incremental_map_preview import (CONFIRM as MAP_CONFIRM, CONTINUE as MAP_CONTINUE,
                                         child_continue_request, confirmation_request, recovery_records)

    root = root_for(request)
    run, control = read(root / 'run.json'), read(root / 'control/head.json')
    successor = attempt.get('supplement_run_id')
    target_id = request['operation_id']
    if successor and attempt['action'] != MAP_CONTINUE:
        from incremental_projection import linked_supplements

        child = linked_supplements(root, run, recovery_records(root)).get(successor)
        if not child or attempt['action'] not in SUPPLEMENT_CONTROLS:
            raise ValueError('补跑运行或控制范围已变化，请刷新后重试。')
        root, run, control, target_id = child['root'], child['run'], child['control'], successor
        if attempt['action'] == 'incremental-continue' and (control.get('boundary') or {}).get('stage') != 'manual':
            raise ValueError('当前补跑停点需要专用恢复入口。')
    same_revisions = (control['revision'] == attempt['control_revision'] and run['revision'] == attempt['run_revision'])
    if attempt['action'] == 'incremental-continue-round':
        binding = {key: attempt[key] for key in ('round_id', 'boundary_fingerprint')}
        operation = {'run_id': target_id, 'actor': request['actor'], 'action': 'continue',
                     'expected_revision': attempt['control_revision'], 'boundary_fingerprint': binding['boundary_fingerprint'],
                     'network_recovery': {'round_id': binding['round_id'], 'run_revision': attempt['run_revision']}}
        operation_id = 'network-continue-' + fingerprint(operation)
        operation['operation_id'] = operation_id
        if not same_revisions or binding != network_continue_binding(run, control):
            saved = read(root / 'control/operations' / (operation_id + '.json'))
            if (run.get('state') != 'active' or run.get('active_round_id') != binding['round_id']
                    or control.get('state') != 'active'
                    or control.get('network_continuation', {}).get('operation_id') != operation_id
                    or saved.get('commit_state') not in ('prepared', 'committed')
                    or saved.get('request_sha256') != fingerprint(operation)):
                raise ValueError('等待网络恢复的轮次已经变化，请刷新后重试。')
        path = directory / 'attempts' / attempt['id'] / 'incremental-request.json'
        save(path, operation)
        return args + ['control', '--request', str(path)]
    if not same_revisions:
        raise ValueError('增量运行或控制修订已经变化，请刷新后重试。')
    if attempt['action'] in (MAP_CONFIRM, MAP_CONTINUE):
        confirming = attempt['action'] == MAP_CONFIRM
        operation = (confirmation_request(directory, request, run, control, attempt)
                     if confirming else child_continue_request(root, request, attempt))
        path = directory / 'attempts' / attempt['id'] / 'incremental-request.json'
        save(path, operation)
        return args + ['recover-map' if confirming else 'control', '--request', str(path)]
    operation = {'run_id': target_id, 'actor': request['actor'], 'operation_id': attempt['id']}
    if attempt['action'] in ('incremental-closure-preview', 'incremental-closure-confirm'):
        if attempt['action'] == 'incremental-closure-preview':
            boundary = control.get('boundary')
            operation.update(operation_id='closure-review-' + fingerprint([run['run_id'], run['revision']])[:40],
                             expected_revision=control['revision'], run_revision=run['revision'],
                             boundary_fingerprint=fingerprint(boundary) if boundary else None)
        else:
            review = read(directory / 'attempts' / attempt['preview_attempt'] / 'closure-preview.json')
            if (review.get('report_sha256') != attempt['report_sha256']
                    or (successor and review.get('request', {}).get('run_id') != target_id)):
                raise ValueError('结案预览已变化，请重新核对。')
            operation = {**review['request'], 'expected_report_sha256': review['report_sha256']}
        path = directory / 'attempts' / attempt['id'] / 'incremental-request.json'
        save(path, operation)
        return args + ['recover-closure', '--request', str(path)] + (
            ['--preview'] if attempt['action'] == 'incremental-closure-preview' else [])
    if 'scope_token' in attempt:
        from incremental_node_recovery import validate_action

        snapshot = progress(directory, request, [{'id': attempt['prior_attempt']}], False, False, True,
                            legacy_map_retry=attempt.get('retry_kind') == 'mapping-unconsumed')
        option = next((action for node in snapshot['nodes'] for action in node['actions']
                       if action['id'] == attempt['action'] and action['node'] == attempt['node']), None)
        if option is None:
            raise ValueError('此节点的恢复条件已经变化，请刷新后重试。')
        validate_action({**attempt, 'attempt': attempt['prior_attempt']}, option, ValueError)
        if option.get('retry_kind') != attempt.get('retry_kind') or option.get('stop_after') != attempt.get('stop_after'):
            raise ValueError('恢复方式已经变化，请刷新后重试。')
    if attempt['action'] == 'incremental-reconcile':
        operation['expected_revision'] = run['revision']
        mode = 'reconcile'
    elif attempt['action'] == 'incremental-node-coordination':
        operation.update(expected_revision=run['revision'], round_id=attempt['round_id'],
                         control_revision=control['revision'])
        mode = 'recover-coordination'
    elif attempt['action'] == 'incremental-node-writing':
        operation.update(expected_revision=control['revision'], run_revision=run['revision'],
                         round_id=attempt['round_id'], boundary_fingerprint=fingerprint(control['boundary']))
        if 'revise_fragments' in attempt:
            operation['revise_fragments'] = attempt['revise_fragments']
        mode = 'recover-writing'
    elif attempt['action'] == 'incremental-node-delivery':
        operation.update(expected_revision=run['revision'], round_id=attempt['round_id'], disposition='continue',
                         stop_after=attempt['stop_after'], control_revision=control['revision'])
        mode = 'recover-delivery'
    elif attempt['action'] == 'incremental-retry-source':
        from incremental_projection import retry_capability
        path, digest = attempt.get('source_path'), attempt.get('source_sha256')
        manifest = next((row for row in run.get('source_manifest', []) if row['path'] == path), None)
        item = run.get('sources', {}).get(path)
        expected_node = 'incremental-source-' + fingerprint({'path': path, 'sha256': digest})[:24]
        capability = retry_capability(item, root, run, control) if item else {}
        if 'scope_token' not in attempt and (not manifest or manifest['sha256'] != digest or attempt.get('node') != expected_node
                or not capability.get('allowed') or capability.get('retry_kind') != attempt.get('retry_kind')):
            raise ValueError('此来源没有可用的受控重试入口，请刷新并检查失败原因。')
        operation.update(control_revision=control['revision'], run_revision=run['revision'], source_path=path,
                         source_sha256=digest, retry_kind=attempt['retry_kind'])
        mode = 'retry-source'
    else:
        operation.update(expected_revision=control['revision'], action=attempt['action'].removeprefix('incremental-').removeprefix('node-'))
        if operation['action'] == 'continue':
            operation['boundary_fingerprint'] = fingerprint(control['boundary'])
        elif operation['action'] == 'release-source':
            source_path, source_sha = attempt.get('source_path'), attempt.get('source_sha256')
            admission = control.get('manual_source_admission', {}).get(source_path)
            expected_node = 'incremental-source-' + fingerprint({'path': source_path, 'sha256': source_sha})[:24]
            if (not admission or admission.get('state') != 'held' or admission.get('sha256') != source_sha
                    or attempt.get('node') != expected_node):
                raise ValueError('来源身份或手动处理许可已经变化，请刷新后重试。')
            operation.update(source_path=source_path, source_sha256=source_sha)
        mode = 'control'
    path = directory / 'attempts' / attempt['id'] / 'incremental-request.json'
    save(path, operation)
    return args + [mode, '--request', str(path)]
