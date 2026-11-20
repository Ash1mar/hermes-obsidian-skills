"""Bind file-node operations to their exact file or shared round."""
from incremental import fingerprint
from incremental_projection import read


def preparation_retry(item, root, run, control, phase):
    task = item['native_preparation']
    if (run['state'] != 'active' or control.get('state') != 'active' or item['state'] != 'pending'
            or (item.get('admission') or {}).get('state') == 'held' or phase.get('error_code')
            or task.get('status') not in ('blocked', 'scheduled') or task.get('claimed')
            or not task.get('last_outcome') or task['last_outcome'] == 'completed'):
        return False
    return not (root / 'preparation-retries' / (fingerprint(item['path']) + '.json')).is_file()


def source_actions(item, root, run, control, rounds, latest, *, writable, interrupted):
    """Project review scopes even when the original run is terminal."""
    identity = {'source_path': item['path'], 'source_sha256': item['source_sha256']}
    node = 'incremental-source-' + fingerprint({'path': item['path'], 'sha256': item['source_sha256']})[:24]
    boundary = control.get('boundary') or {}
    boundary_scope = boundary.get('scope', {})
    paused = control.get('state') == 'paused' and not control.get('active_scopes')
    active = control.get('state') == 'active'
    for phase in item['pipeline']:
        key = phase['key']
        phase['actions'] = []

        def offer(scope, action_id, label, **fields):
            if not writable:
                return
            rid = scope.get('round_id')
            binding = {'phase': key, 'round_id': rid, 'scope': scope, **identity}
            action = {'id': action_id, 'label': label, 'node': f'{node}-{key}' + (f'-{rid}' if rid else ''),
                      'attempt': latest, 'control_revision': control['revision'], 'run_revision': run['revision'],
                      'phase': key, 'round_id': rid, 'scope_token': fingerprint(binding), **identity, **fields}
            phase['actions'].append(action)
            item['actions'].append(action)

        if key == 'prepare':
            if phase.get('task_id') or phase.get('detail'):
                phase['recovery_scopes'] = [{k: phase.get(k) for k in ('detail', 'error_code', 'task_id', 'evidence_refs')}]
            if preparation_retry(item, root, run, control, phase):
                offer({'task_id': phase['task_id']}, 'incremental-retry-source', '重试此文件转换',
                      retry_kind='preparation-unfinished')
        elif key == 'mapping':
            snapshot = item.get('snapshot_id')
            phase['recovery_scopes'] = [{'snapshot_id': snapshot, 'map_result_id': item.get('map_result_id')}] if snapshot else []
            if item['retry']['allowed'] and (paused or interrupted):
                offer({'snapshot_id': snapshot, 'request_ids': sorted(item['retry']['request_ids'])},
                      'incremental-retry-source', '恢复此文件映射', retry_kind=item['retry']['retry_kind'])
            if paused and boundary.get('stage') == key and boundary_scope == {'scope_kind': 'source', 'scope_id': snapshot}:
                offer({'snapshot_id': snapshot}, 'incremental-node-continue', '从此映射停点继续')
        elif key in ('coordination', 'writing', 'formal-build', 'release', 'acceptance'):
            phase['recovery_scopes'] = []
            for related in item['related_rounds']:
                rid = related['round_id']
                round_ = rounds[rid]
                scope = round_scope(root, key, round_, related)
                phase['recovery_scopes'].append(scope)
                if rid != run.get('active_round_id'):
                    continue
                at_boundary = paused and boundary_scope == {'scope_kind': 'round', 'scope_id': rid}
                if at_boundary and boundary.get('stage') == key:
                    name = {'coordination': '从此协调停点继续', 'writing': '复用写作成果并继续',
                            'formal-build': '复用构建成果并继续', 'release': '继续发布与索引', 'acceptance': '继续验收'}[key]
                    offer(scope, 'incremental-node-continue', name)
                    candidates = [row['request_id'] for row in scope.get('fragments', []) if row['reviewable']]
                    if key == 'writing' and candidates and key in control.get('pause_after', []):
                        offer(scope, 'incremental-node-writing', '恢复选中的受阻分片',
                              fragment_ids=candidates)
                if (paused and key == 'writing' and boundary.get('stage') == 'manual'
                        and 'writing' in control.get('pause_after', [])
                        and rid in control.get('writing_repairs', {}) and rid not in control.get('writing_continuations', {})):
                    writer = root / round_['writing_workspace']
                    if (writer / 'stop.json').is_file() and not (root / 'rounds' / rid / 'writing-revision.json').is_file():
                        offer(scope, 'incremental-node-writing', '接续中断的本轮写作')
                if active and interrupted and key == 'coordination' and not (root / 'rounds' / rid / 'page-changes.json').exists():
                    if not (root / 'native/round' / rid / 'coordination/stop.json').exists():
                        offer(scope, 'incremental-node-coordination', '恢复此轮协调')
                if active and interrupted and key in ('formal-build', 'release', 'acceptance'):
                    recovery = delivery_recovery(root, key, round_)
                    if recovery is not None:
                        offer(scope, 'incremental-node-delivery',
                              {'formal-build': '恢复正式构建', 'release': '恢复交付', 'acceptance': '恢复验收'}[key],
                              **recovery)


def round_scope(root, key, round_, related):
    rid = round_['round_id']
    scope = {'round_id': rid, 'role': related['role'], 'page_count': len(related['dependent_page_ids'])}
    if key in ('coordination', 'writing'):
        dependencies = set(related['dependent_change_ids'])
        held = {row['change_id']: row.get('reason') for row in round_['writing_held'] if 'change_id' in row}
        pages = [row for row in round_['page_changes'] if row.get('change_id') in dependencies]
        scope['blocked_pages'] = [{'name': row.get('name') or row.get('page_id'), 'change_id': row['change_id'],
                                   'reason': row.get('reason') if key == 'coordination' else held[row['change_id']]}
                                  for row in pages if (row.get('action') == 'hold' if key == 'coordination' else row['change_id'] in held)]
        if key == 'coordination':
            scope['shared_gap_count'] = len(round_['coordination_gaps'])
        else:
            writer = root / round_['writing_workspace']
            fragments = {}
            for page in scope['blocked_pages']:
                work = read(writer / 'fragment-plans' / (page['change_id'] + '.json'))
                for index, part in enumerate(work.get('fragments', []), 1):
                    identity = part['request']['id']
                    result = read(writer / 'jobs' / (identity + '.result.json'))
                    fragments[identity] = {'request_id': identity, 'name': page['name'], 'ordinal': index,
                                           'reviewable': result.get('state') == 'settled' and len(result.get('attempts', [])) == 1}
            scope['fragments'] = list(fragments.values())
    else:
        scope.update(build_run_id=round_['build_run_id'], release_id=round_['release_id'],
                     provider_generation=round_['provider_generation'], delivery_state=round_['delivery_state'])
    return scope


def delivery_recovery(root, phase, round_):
    rid = round_['round_id']
    state = round_['delivery_state']
    if not (root / 'rounds' / rid / 'formal-request.json').is_file():
        return None
    finishes_build = state in ('prepared', 'build_committing')
    finishes_release = state in ('build_committed', 'release_applying', 'release_applied', 'index_pending')
    if ((phase == 'formal-build' and finishes_build) or (phase == 'release' and finishes_release)
            or (phase == 'acceptance' and state == 'index_verified')):
        return {'stop_after': 'build_committed' if finishes_build else
                'index_verified' if finishes_release else None}
    return None


def validate_action(body, option, error):
    keys = ('node', 'attempt', 'source_path', 'source_sha256', 'phase', 'round_id', 'scope_token')
    if any(body.get(key) != option.get(key) for key in keys):
        raise error('恢复节点、来源或共享轮次已经变化，请刷新后重试。')
    selected = body.get('revise_fragments')
    if 'fragment_ids' in option:
        if (not isinstance(selected, list) or not selected or any(not isinstance(key, str) for key in selected)
                or len(selected) != len(set(selected)) or not set(selected) <= set(option['fragment_ids'])):
            raise error('请选择此文件依赖范围内的受阻写作分片。')
    elif selected is not None:
        raise error('当前节点不接收写作分片。')
    return {key: option[key] for key in ('phase', 'round_id', 'scope_token', 'source_path', 'source_sha256', 'stop_after')
            if key in option} | ({'revise_fragments': selected} if selected else {})
