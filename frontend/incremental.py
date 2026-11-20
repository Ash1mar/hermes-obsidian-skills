"""Console projection for explicit source/round incremental runs."""

from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import sys


MODE = 'incremental_v1'
PAUSES = ('prepare', 'mapping', 'coordination', 'writing', 'formal-build', 'release', 'acceptance', 'closure')
DEFAULT_BUDGET = {'max_calls': 600, 'max_input_tokens': 12000000, 'max_output_tokens': 3000000,
                  'phase_limits': {'mapping': 360, 'coordination': 30, 'writing': 210}}
DEFAULT_POLICY = {'max_maps_per_round': 1}


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


def progress(directory, request, attempts, active, historical, details):
    root = root_for(request)
    run = read(root / 'run.json')
    control = read(root / 'control/head.json')
    controller = read(root / 'controller.json')
    controller_active = None
    if os.name == 'posix' and controller.get('pid'):
        try:
            proc = Path('/proc') / str(int(controller['pid']))
            controller_active = controller.get('start') == (proc / 'stat').read_text().split(') ', 1)[1].split()[19]
        except (FileNotFoundError, ValueError, IndexError):
            controller_active = False
    latest = attempts[-1] if attempts else {}
    if run.get('state') in ('completed', 'partial'):
        state = run['state']
    elif control.get('state') in ('paused', 'pausing', 'cancelled'):
        state = control['state']
    elif latest.get('returncode', 0) and not run:
        state = 'failed'
    elif run and controller_active is False and not active:
        state = 'interrupted'
    else:
        state = 'running' if run or active else 'pending'
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
    manual = control.get('manual_source_admission', {})
    sources = [{'name': Path(path).name, 'path': path, **row,
                'admission': manual.get(path), 'actions': []} for path, row in run.get('sources', {}).items()]
    counts = dict(Counter(row['state'] for row in sources))
    actions = []
    if not historical and run and run['state'] == 'active' and not active:
        if control.get('state') == 'paused':
            actions.append({'id': 'incremental-continue', 'label': '继续当前作用域'})
        elif control.get('state') == 'active':
            if not control.get('active_scopes'):
                actions.append({'id': 'incremental-pause', 'label': '暂停增量运行', 'gateway_required': False})
            actions.append({'id': 'incremental-reconcile', 'label': '接管并核对未完成工作'})
    for action in actions:
        action.update(node='incremental-control', attempt=latest.get('id'), control_revision=control.get('revision'),
                      run_revision=run.get('revision'))
    source_nodes = []
    for item in sources:
        source_pipeline(item, root, run, control, source, rounds, native_reader=native_reader)
        admission = item['admission']
        node = 'incremental-source-' + fingerprint({'path': item['path'], 'sha256': item.get('source_sha256')})[:24]
        if (admission and admission.get('state') == 'held' and not historical and not active
                and run.get('state') == 'active' and control.get('state') in ('active', 'paused', 'pausing')):
            item['actions'].append({'id': 'incremental-release-source', 'node': node,
                'label': '开始处理此文件' if control['state'] == 'active' else '允许处理（继续后开始）',
                'attempt': latest.get('id'), 'control_revision': control.get('revision'),
                'run_revision': run.get('revision'), 'source_path': item['path'],
                'source_sha256': admission['sha256']})
        source_actions(item, root, run, control, rounds_by_id, latest.get('id'),
                       writable=not historical and not active and run.get('state') == 'active',
                       interrupted=controller_active is not True)
        if admission or item['actions']:
            source_nodes.append({'key': node, 'name': item['name'], 'state': item['state'],
                                 'description': '逐文件手动准入；允许处理不会释放暂停边界。',
                                 'actions': item['actions']})
    released = read(Path(request['vault_path']) / '_system/metadata/knowledge-release-state.json').get('current_release_id')
    index = read(Path(request['vault_path']) / '_system/reports/retrieval-index-manifest.json')
    delivery_window = read(Path(request['vault_path']) / '_system/metadata/incremental-delivery-state.json')
    delivering = bool(delivery_window and delivery_window.get('state') not in ('committed', 'aborted'))
    data.update(nodes=[{'key': 'incremental-control', 'name': '增量运行控制', 'state': state,
                       'description': '暂停作用于整个增量运行。继续只释放当前已核验的边界。', 'actions': actions}, *source_nodes],
                incremental={'sources': sources, 'source_counts': counts, 'rounds': rounds,
                             'overall_stages': overall_stages(sources, rounds, run, control),
                             'active_round_id': run.get('active_round_id'), 'control': control,
                             'budget': run.get('budget'), 'controller': controller,
                             'controller_active': controller_active, 'delivery_window': delivery_window},
                attempts=attempts, sources=request['sources'], active_node=None,
                workflow_id=source.get('workflow_id'), source_revision=source.get('revision'),
                source_stage=source.get('current_stage'), source_board=source.get('kanban', {}).get('board_id'),
                short_board=None, pause=control.get('boundary', {}).get('stage') if control.get('boundary') else None,
                operator={}, source_wait={}, pause_request={}, receipts=len(run.get('map_result_ids', [])),
                release_id=released, indexed=bool(not delivering and released and index.get('release_id') == released and index.get('status') == 'ready'),
                query_enabled=request['retrieval_policy']['query_enabled'])
    return data


def command(directory, attempt, config, save):
    request = read(directory / 'request.json')
    script = Path(config['skills_root']) / 'hermes-obsidian-governed-ingest-orchestrator/scripts/manage_incremental_ingest.py'
    if not script.is_file():
        raise ValueError('已安装的 Skill 缺少增量入口。')
    args = [sys.executable, '-B', str(script), '--vault', request['vault_path']]
    if attempt['action'] == 'start':
        return args + ['start', '--request', str(directory / 'request.json'), '--record', str(directory / 'incremental-start.json'),
                       '--bootstrap-skill', str(Path(config['skills_root']) / 'hermes-obsidian-vault-bootstrap')]
    root = root_for(request)
    run, control = read(root / 'run.json'), read(root / 'control/head.json')
    if control['revision'] != attempt['control_revision'] or run['revision'] != attempt['run_revision']:
        raise ValueError('增量运行或控制修订已经变化，请刷新后重试。')
    operation = {'run_id': request['operation_id'], 'actor': request['actor'], 'operation_id': attempt['id']}
    if 'scope_token' in attempt:
        from incremental_node_recovery import validate_action

        snapshot = progress(directory, request, [{'id': attempt['prior_attempt']}], False, False, True)
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
