"""Project and bind a read-only assessment before any scoped Map recovery."""
import hashlib
import re

from incremental import fingerprint, read


PREVIEW = 'incremental-map-recovery-preview'
CONFIRM = 'incremental-map-recovery-confirm'
CONTINUE = 'incremental-map-supplement-continue'
STRATEGIES = {
    'original': '确认恢复原 Map 卡',
    'supplemental': '确认独立补做 Map',
}
SHA256 = re.compile(r'[0-9a-f]{64}')
RUN_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,95}')
PREVIEW_ID = re.compile(r'map-recovery-[0-9a-f]{40}')
CHILD_BINDING = ('supplement_run_id', 'supplement_run_revision', 'supplement_control_revision',
                 'boundary_fingerprint', 'recovery_operation_id')


def recovery_request(request, run, control, source_path, source_sha256, error):
    exact = next((row for row in run.get('source_manifest', []) if row['path'] == source_path), None)
    if (not exact or exact['sha256'] != source_sha256 or source_path not in run.get('sources', {})
            or run.get('run_id') != request['operation_id'] or not SHA256.fullmatch(source_sha256 or '')):
        raise error('Map 恢复范围与原来源身份不一致。')
    binding = {'run_id': request['operation_id'], 'actor': request['actor'], 'run_revision': run['revision'],
               'control_revision': control['revision'], 'source_path': source_path, 'source_sha256': source_sha256}
    return {**binding, 'operation_id': 'map-recovery-' + fingerprint(binding)[:40]}


def preview_path(directory, operation):
    key = fingerprint([operation['source_path'], operation['source_sha256']])
    return directory / 'map-recovery-previews' / (key + '.json')


def review_binding(review, operation, *, launched=False):
    if not isinstance(review, dict):
        return None
    bound = review.get('request')
    digest = review.get('report_sha256')
    if (not isinstance(bound, dict) or set(bound) != set(operation)
            or not isinstance(bound.get('operation_id'), str) or not PREVIEW_ID.fullmatch(bound['operation_id'])
            or not isinstance(digest, str) or not SHA256.fullmatch(digest)
            or not isinstance(review.get('strategies'), list) or not isinstance(review.get('outcomes'), list)
            or fingerprint({key: value for key, value in review.items() if key != 'report_sha256'}) != digest):
        return None
    revisions = ('run_revision', 'control_revision')
    if any(type(bound[key]) is not int for key in revisions):
        return None
    if any(bound[key] != operation[key] for key in operation if key not in (*revisions, 'operation_id')):
        return None
    if launched and (bound['operation_id'] != operation['operation_id']
                     or any(bound[key] < operation[key] for key in revisions)):
        return None
    if not launched and any(bound[key] != operation[key] for key in revisions):
        return None
    return bound


def strategy_for(review, name):
    if name not in STRATEGIES:
        return None
    matching = [row for row in review['strategies'] if isinstance(row, dict) and row.get('id') == name]
    return matching[0] if len(matching) == 1 else None


def allowed_request_ids(strategy):
    ids = (strategy or {}).get('request_ids')
    if (not strategy or strategy.get('allowed') is not True or not isinstance(ids, list)
            or any(not isinstance(key, str) or not SHA256.fullmatch(key) for key in ids)):
        return []
    return sorted(set(ids))


def validate_action(body, option, error):
    keys = ('node', 'attempt', 'control_revision', 'run_revision', 'source_path', 'source_sha256',
            'preview_key', 'report_sha256', 'strategy')
    if option['id'] == CONTINUE:
        keys += CHILD_BINDING
    if any(body.get(key) != option.get(key) for key in keys):
        raise error('Map 恢复范围、运行修订或预览已变化，请重新核对。')
    if option['id'] == CONFIRM and body.get('request_id') not in option['request_ids']:
        raise error('请选择本次预览中可以恢复的原 Map 请求。')


def confirmation_request(directory, request, run, control, attempt):
    operation = recovery_request(request, run, control, attempt['source_path'], attempt['source_sha256'], ValueError)
    review = read(preview_path(directory, operation))
    bound = review_binding(review, operation)
    strategy = attempt.get('strategy')
    request_id = attempt.get('request_id')
    if (not bound or attempt.get('preview_key') != bound['operation_id']
            or attempt.get('report_sha256') != review['report_sha256']
            or request_id not in allowed_request_ids(strategy_for(review, strategy))):
        raise ValueError('Map 恢复预览已变化，请重新核对后确认。')
    return {**bound, 'strategy': strategy, 'request_id': request_id,
            'expected_report_sha256': review['report_sha256']}


def recovery_records(root):
    return [read(path) for path in sorted((root.parent.parent / 'incremental-map-recoveries' / root.name).glob('*.json'))]


def child_continuation(root, child_run, child_control, request, result):
    successor = result['supplement_run_id']
    child = root.parent / successor
    boundary = child_control.get('boundary') or {}
    scope = boundary.get('scope') or {}
    evidence = boundary.get('evidence') or {}
    source = child_run.get('sources', {}).get(request['source_path'], {})
    pointer = child_run.get('map_supplement')
    if (child_run.get('run_id') != successor or child_run.get('state') != 'active'
            or child_control.get('state') != 'paused' or child_control.get('active_scopes')
            or child_run.get('actor') != request.get('actor') or child_control.get('actor') != request.get('actor')
            or boundary.get('stage') != 'mapping' or scope != {'scope_kind': 'source', 'scope_id': source.get('snapshot_id')}
            or not source.get('snapshot_id') or source.get('state') != 'mapped'
            or result.get('map_result_id') != source.get('map_result_id')
            or source.get('map_result_id') != evidence.get('map_result_id')
            or source.get('map_result_id') not in child_run.get('map_result_ids', [])
            or not pointer or evidence.get('map_supplement') != pointer
            or result.get('boundary_fingerprint') != fingerprint(boundary)
            or result.get('run_revision') != child_run.get('revision')
            or result.get('control_revision') != child_control.get('revision')):
        return None
    manifest = child / 'map-supplement.json'
    if pointer.get('ref') != manifest.relative_to(root.parents[3]).as_posix() or not manifest.is_file():
        return None
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != pointer.get('sha256'):
        return None
    lineage = read(manifest)
    original = lineage.get('request', {})
    binding = ('run_id', 'actor', 'operation_id', 'source_path', 'source_sha256', 'run_revision', 'control_revision')
    if (lineage.get('contract') != 'incremental-map-supplement/v1' or lineage.get('supplement_run_id') != successor
            or any(original.get(key) != request.get(key) for key in binding)
            or original.get('request_ids') != [request['request_id']]):
        return None
    return {'supplement_run_id': successor, 'supplement_run_revision': child_run['revision'],
            'supplement_control_revision': child_control['revision'], 'boundary_fingerprint': fingerprint(boundary),
            'recovery_operation_id': request['operation_id']}


def child_continue_request(root, request, attempt):
    candidates = linked_results(root, recovery_records(root), attempt['source_path'], attempt['source_sha256'])
    child = next((row['continuation'] for row in candidates if row.get('continuation')
                  and all(row['continuation'].get(key) == attempt.get(key) for key in CHILD_BINDING)), None)
    if not child:
        raise ValueError('补做运行的 Map 停点已变化，请重新核对。')
    return {'run_id': child['supplement_run_id'], 'actor': request['actor'], 'action': 'continue',
            'operation_id': 'map-supplement-continue-' + fingerprint(child)[:40],
            'expected_revision': child['supplement_control_revision'],
            'expected_run_revision': child['supplement_run_revision'],
            'boundary_fingerprint': child['boundary_fingerprint']}


def linked_results(root, records, source_path, source_sha256):
    output = []
    for record in records:
        request = record.get('request', {})
        if (record.get('contract') != 'incremental-map-operator-recovery/v1'
                or record.get('state') not in ('prepared', 'committed') or request.get('run_id') != root.name
                or request.get('source_path') != source_path or request.get('source_sha256') != source_sha256):
            continue
        result = record.get('receipt') or {}
        strategy = request.get('strategy')
        successor = result.get('supplement_run_id')
        if strategy not in STRATEGIES or (result and result.get('strategy') != strategy):
            continue
        row = {'strategy': strategy, 'state': result.get('state') or record['state'],
               'request_id': request.get('request_id')}
        if strategy == 'supplemental' and isinstance(successor, str) and RUN_ID.fullmatch(successor):
            child = root.parent / successor
            child_run = read(child / 'run.json')
            child_control = read(child / 'control/head.json')
            state = child_control.get('state') if child_run.get('state') == 'active' else child_run.get('state')
            row.update(supplement_run_id=successor, state=state or row['state'],
                       boundary=(child_control.get('boundary') or {}).get('stage'),
                       map_result_ids=child_run.get('map_result_ids', []))
            if record['state'] == 'committed':
                if continuation := child_continuation(root, child_run, child_control, request, result):
                    row['continuation'] = continuation
        output.append(row)
    return output


def project_map_recovery(directory, root, request, run, control, item, latest, records, *, preview, confirm):
    phase = next(row for row in item['pipeline'] if row['key'] == 'mapping')
    path, digest = item['path'], item.get('source_sha256')
    phase['map_recovery'] = {'results': linked_results(root, records, path, digest)}
    if not digest or 'revision' not in control:
        return
    try:
        operation = recovery_request(request, run, control, path, digest, ValueError)
    except ValueError:
        return
    node = 'incremental-source-' + fingerprint({'path': path, 'sha256': digest})[:24] + '-mapping'
    if not phase['recovery_scopes']:
        phase['recovery_scopes'].append({})
    action = {key: operation[key] for key in ('source_path', 'source_sha256', 'run_revision', 'control_revision')}
    action.update(node=node, phase='mapping', round_id=None, attempt=latest.get('id'),
                  preview_key=operation['operation_id'])

    def offer(option):
        phase['actions'].append(option)
        item['actions'].append(option)

    if preview:
        offer({**action, 'id': PREVIEW, 'label': '预览 Map 恢复与补做', 'gateway_required': False})
    if confirm:
        for result in phase['map_recovery']['results']:
            if child := result.get('continuation'):
                offer({**action, **child, 'id': CONTINUE, 'label': '继续补充轮次',
                       'node': node + '-continue-' + child['supplement_run_id']})
    review = read(preview_path(directory, operation))
    bound = review_binding(review, operation)
    if not bound:
        return
    phase['map_recovery'].update(report_sha256=review['report_sha256'], outcomes=review['outcomes'], strategies=[])
    for strategy, label in STRATEGIES.items():
        option = strategy_for(review, strategy)
        if option is None:
            continue
        ids = allowed_request_ids(option)
        phase['map_recovery']['strategies'].append({
            'id': strategy, 'allowed': bool(ids), 'reason': option.get('reason'), 'request_ids': ids,
        })
        if confirm and ids:
            offer({**action, 'id': CONFIRM, 'label': label, 'node': node + '-' + strategy,
                   'strategy': strategy, 'request_ids': ids, 'report_sha256': review['report_sha256'],
                   'preview_key': bound['operation_id']})
