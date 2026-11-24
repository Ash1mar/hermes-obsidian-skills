"""Review and authorize one original Map request through its appropriate recovery path."""
import re

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_sources import safe_id
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import service
from short_evidence_pass import require


PREVIEW_FIELDS = {'run_id', 'actor', 'operation_id', 'run_revision', 'control_revision', 'source_path', 'source_sha256'}
CONFIRM_FIELDS = {'strategy', 'request_id', 'expected_report_sha256'}
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
                    exact = recovery_inventory(domain, run_id, source_path, request_ids=[request_id])
                    require(exact['mode'] == mode and any(r['request_id'] == request_id for r in exact['eligible']),
                            'original Map request changed while reviewing')
                    selected.update(retry_kind='mapping-unconsumed', evidence_fingerprint=exact['evidence_fingerprint'])
                else:
                    plan = supplement(domain.vault, selected, preview=True)
                    selected['expected_report_sha256'] = plan['report_sha256']
                    item['supplement_run_id'] = plan.get('supplement_run_id')
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
    document = {'request': bound, 'strategies': strategies, 'outcomes': outcomes, 'mode': mode,
                'review_status': 'ready' if candidates else 'unavailable',
                'evidence_fingerprint': evidence['evidence_fingerprint'],
                'execution_intents': {key: fingerprint(payload) for key, payload in candidates.items()}}
    return {**document, 'report_sha256': fingerprint(document)}, candidates


def recover(vault, request, *, preview=False):
    """Review any run state, then revalidate and durably authorize one card."""
    from incremental_map_supplement import supplement
    from incremental_source_retry import retry

    require(set(request) == PREVIEW_FIELDS | (set() if preview else CONFIRM_FIELDS),
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
            if saved['state'] == 'committed':
                return saved['receipt']
        else:
            base = {key: request[key] for key in PREVIEW_FIELDS}
            report, candidates = review(domain, base)
            require(report['request'] == base and report['report_sha256'] == request['expected_report_sha256'],
                    'Map recovery preview changed. Review the current evidence.')
            require(report['mode'] == strategy and request_id in candidates,
                    'selected Map request no longer qualifies for this strategy')
            saved = {'contract': 'incremental-map-operator-recovery/v1', 'request': request,
                     'execution_intent': candidates[request_id], 'state': 'prepared'}
            _write_atomic(path, _json_bytes(saved))

        selected = saved['execution_intent']
        result = (retry(domain.vault, selected) if strategy == 'original'
                  else supplement(domain.vault, selected, preview=False))
        receipt = {**result, 'strategy': strategy, 'request_id': request_id, 'source_path': request['source_path']}
        _write_atomic(path, _json_bytes({**saved, 'state': 'committed', 'receipt': receipt}))
        return receipt


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
