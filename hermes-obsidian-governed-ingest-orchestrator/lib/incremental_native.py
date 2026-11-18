"""Use the existing Hermes native request runner for incremental scopes."""
from contextlib import closing
from pathlib import Path
import json

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_ingest import FileIncrementalIngestService
from hermes_source_units.incremental_sources import completion_connection, pinned, read_pin
from hermes_source_units.source_units import _json_bytes, _load_json, _sha, _vault_path, _write_atomic
from hermes_source_units.validation import _check_shape, fingerprint
from short_evidence_pass import require


def service(vault, run_id):
    return FileIncrementalIngestService(vault, check_admission=IncrementalControl(vault, run_id).check_admission)


def mutate(domain, run_id, method, operation_id, **fields):
    """Serialize durable domain mutations across the independently running sources."""
    from hermes_source_units.source_units import _exclusive_lock
    from hermes_source_units.incremental_sources import safe_id

    with _exclusive_lock(domain.root(run_id) / '.runtime-mutation.lock'):
        safe_id(operation_id)
        run = domain.status(run_id)
        path = domain.root(run_id) / 'runtime-requests' / (operation_id + '.json')
        request = {'run_id': run_id, 'actor': run['actor'], 'operation_id': operation_id,
                   'expected_revision': run['revision'], **fields}
        if path.exists():
            saved = _load_json(path)
            require(saved['method'] == method and
                    {k: v for k, v in saved['request'].items() if k != 'expected_revision'} ==
                    {k: v for k, v in request.items() if k != 'expected_revision'},
                    'runtime operation key identifies different inputs')
            request = saved['request']
            journal = domain.root(run_id) / 'operations' / (operation_id + '.json')
            if not journal.exists() and request['expected_revision'] != run['revision']:
                request = {**request, 'expected_revision': run['revision']}
                _write_atomic(path, _json_bytes({'method': method, 'request': request}))
        else:
            _write_atomic(path, _json_bytes({'method': method, 'request': request}))
        return getattr(domain, method)(request)


def check_operation(root, operation):
    require(operation.get('workflow_mode') == 'incremental_v1', 'incremental execution must explicitly opt in')
    value = operation['incremental']
    domain = service(Path(operation['vault']), value['run_id'])
    run = domain.status(value['run_id'])
    require(run['state'] == 'active' and run['actor'] == operation['actor'], 'incremental run has closed or changed actor')
    IncrementalControl(domain.vault, value['run_id']).check_admission(value)
    source = _load_json(_vault_path(domain.vault, f"_system/ledgers/ingest-workflows/{run['source_workflow_id']}.json"))
    require(not source['cancel_requested'] and source['actor'] == run['actor']
            and source['scope'].get('incremental_run_id') == run['run_id'], 'incremental source execution stopped')
    for snapshot_id in value['snapshots']:
        domain.validate_source_snapshot(run['run_id'], snapshot_id)
    require(root.resolve().is_relative_to(domain.root(run['run_id'])), 'native workspace escaped its run')


def reserve_call(root, row, ordinal, grant):
    operation = _load_json(root / 'operation.json')
    scope = operation['incremental']
    domain = service(Path(operation['vault']), scope['run_id'])
    # Unknown calls remain charged against these reservations after a restart.
    reservation = {'request_fingerprint': fingerprint({'request': row['id'], 'ordinal': ordinal}),
                   'phase': scope['stage'], 'max_calls': 1,
                   'max_input_tokens': row['budget'].get('text_tokens') or row['budget']['utf8_bytes'],
                   'max_output_tokens': row['output_cap']}
    return mutate(domain, scope['run_id'], 'reserve_budget', 'call-' + reservation['request_fingerprint'][:64],
                  reservation=reservation, scope_kind=scope['scope_kind'], scope_id=scope['scope_id'])


def seal_response(root, row, response):
    _check_shape(row['schema'], response, {}, '$')
    answer = response['results']['r']
    record = {'contract': 'incremental-native-response/v1', 'request_fingerprint': row['id'], 'answer': answer}
    path = root / 'receipts' / (row['id'] + '.json')
    from prepare_short_evidence_test import save

    save(path, record)
    return {'sealed': ['r'], 'held': {}}


def verified_result(root, row):
    """Recheck ended native identity and the exact audited model response."""
    import run_short_evidence_test as runner
    from fixed_semantic_recovery import transcript_reply

    job = root / 'jobs' / (row['id'] + '.json')
    result_path = job.with_suffix('.result.json')
    result = _load_json(result_path)
    operation = _load_json(root / 'operation.json')
    vault = Path(operation['vault']).resolve()
    require(operation.get('workflow_mode') == 'incremental_v1' and root.resolve().is_relative_to(vault)
            and result.get('state') in ('settled', 'held') and isinstance(result.get('attempts'), list),
            'incremental result is outside its immutable native workspace')
    kb, kc, _ = runner.runtime()
    with closing(completion_connection(operation['board'])) as connection:
        task = kb.get_task(connection, result['task_id'])
        require(task and task.created_by == operation['contract']
                and task.status in ('done', 'archived') and not task.claim_lock
                and task.body == runner.task_body(root, job)
                and any(run.id == result['run_id'] and run.outcome == 'completed'
                        for run in kb.list_runs(connection, task.id, include_active=False)),
                'incremental result lacks native completion')
    require(_load_json(job)['request'] == row and result['request_id'] == row['id'], 'native request identity changed')
    if row['phase'] == 'incremental-formal-build':
        from hermes_source_units.short_build import evidence
        from hermes_source_units.vault_finalize import FileVaultFinalizeService

        scope = operation['incremental']
        service = FileVaultFinalizeService(vault)
        delivery_path = _vault_path(vault,
            f"_system/ledgers/incremental-ingest/{scope['run_id']}/rounds/{scope['scope_id']}/delivery.json")
        current_delivery = _load_json(delivery_path)
        saved = result.get('delivery', {})
        delivery = _load_json(delivery_path.parent / 'delivery-steps' / f"{saved['revision']:04d}.json")
        require(result['state'] == 'settled' and result['attempts'] == []
                and saved == delivery and delivery['state'] in ('build_committed', 'index_verified', 'committed', 'aborted')
                and current_delivery['input_fingerprint'] == delivery['input_fingerprint']
                and delivery['authorization'] == row['view']['authorization']
                and delivery['build_run_id'] == result['formal_run_id'],
                'formal native result differs from its exact committed delivery')
        build_run = _load_json(service.build._run_path(result['formal_run_id']))
        require((delivery['state'] == 'aborted' and build_run['state'] == 'draft'
                 and row['view'].get('recovery', {}).get('disposition') == 'abort_uncommitted')
                or (delivery['state'] != 'aborted' and build_run['state'] == 'completed'),
                'formal native result has an incompatible Build state')
        require(evidence(service.build, build_run)['scope']['authorization'] == delivery['authorization'],
                'formal native result lost its original Build authorization')
        service.build.validate_run(result['formal_run_id'])
    matched_receipt = False
    for attempt in result['attempts']:
        ordinal = attempt['ordinal']
        audit = _load_json(job.with_name(job.stem + f'.audit-{ordinal}.json'))
        reservation = _load_json(job.with_name(job.stem + f'.attempt-{ordinal}.json'))
        require(audit['grant'] == reservation['grant'] and audit['grant']['task_id'] == task.id
                and audit['grant']['run_id'] == result['run_id']
                and audit['grant']['board'] == operation['board']
                and attempt['grant'] == audit['grant'] and attempt['ordinal'] == reservation['ordinal'],
                'native response claim changed')
        if audit['state'] != 'returned':
            continue
        call = reservation['request']
        require(call['id'] == row['id'] or runner.checked_retry(row, call, job, audit),
                'native audit is not the original request or its permitted correction')
        projection = runner.checked_projection(call)
        proof = {'model': audit['model'], 'response_sha256': fingerprint(audit['raw']),
                 'user_sha256': fingerprint(projection['wire']['user']),
                 'image_inputs': [{k: v for k, v in image.items() if k != 'url'} for image in call['image_inputs']]}
        require(audit['model']['parent_native_run_id'] == str(result['run_id'])
                and transcript_reply(proof, projection['wire']) == audit['raw'], 'native transcript differs')
        if result['state'] == 'settled' and 'r' in attempt.get('capture', {}).get('sealed', []):
            from fixed_semantic import decode_semantic_response
            answer, _ = decode_semantic_response(audit['raw'])
            if row['phase'].startswith('incremental-page-'):
                expected = {'contract': 'incremental-native-response/v1', 'request_fingerprint': row['id'],
                            'answer': answer['results']['r']}
            else:
                from short_evidence_pass import receipt
                expected = receipt(row, answer)
            require(_load_json(root / 'receipts' / (row['id'] + '.json')) == expected,
                    'sealed incremental response differs from its native audited output')
            matched_receipt = True
    require(result['state'] != 'settled' or row['phase'] == 'incremental-formal-build' or matched_receipt,
            'settled semantic request has no authenticated sealed response')
    pointer = {'board': operation['board'], 'task_id': task.id, 'run_id': result['run_id'],
               'request': pinned(Path(operation['vault']), job.relative_to(operation['vault']).as_posix()),
               'result': pinned(Path(operation['vault']), result_path.relative_to(operation['vault']).as_posix())}
    return result, pointer


def build(root, job, request, own, runner):
    from hermes_source_units.build_acceptance import assess, record
    from hermes_source_units.incremental_delivery import IncrementalDeliveryService
    from hermes_source_units.incremental_pages import build_scope, validate_page_revision, validate_preparation
    from hermes_source_units.short_build import admit
    from hermes_source_units.vault_finalize import FileVaultFinalizeService

    operation = _load_json(root / 'operation.json')
    view = request['view']
    require(request['phase'] == 'incremental-formal-build' and request['id'] == fingerprint(view)
            and operation.get('workflow_mode') == 'incremental_v1'
            and operation['incremental']['scope_kind'] == 'round'
            and operation['incremental']['stage'] == 'formal-build', 'incremental Build request changed')
    service = FileVaultFinalizeService(operation['vault'])
    binding = own()
    require(binding['board'] == operation['board'], 'incremental Build native board changed')
    recovery = view.get('recovery')
    stop_after = recovery.get('stop_after') if recovery is not None else view.get('stop_after')
    require(stop_after in (None, 'build_committed', 'index_verified'), 'unknown native delivery checkpoint')
    if recovery is not None:
        from hermes_source_units.incremental_sources import safe_id
        require(isinstance(recovery, dict)
                and {'operation_id', 'original_job', 'disposition', 'stop_after'} <= set(recovery)
                and not set(recovery) - {'operation_id', 'original_job', 'disposition', 'stop_after', 'isolation'}
                and recovery['disposition'] in ('continue', 'abort_uncommitted', 'isolate_committed'),
                'unknown native delivery recovery intent')
        require((recovery['disposition'] == 'isolate_committed'
                 and isinstance(recovery.get('isolation'), list) and recovery['isolation'])
                or (recovery['disposition'] != 'isolate_committed' and not recovery.get('isolation')),
                'forward isolation requires its exact reviewed page rows')
        require(recovery['disposition'] != 'abort_uncommitted' or stop_after is None,
                'an aborted window cannot request a later delivery checkpoint')
        safe_id(recovery['operation_id'])
        original_job = read_pin(service.vault, recovery['original_job'])
        original = original_job['request']
        require(original['phase'] == 'incremental-formal-build' and 'recovery' not in original['view']
                and {k: v for k, v in view.items() if k not in ('recovery', 'stop_after')}
                == {k: v for k, v in original['view'].items() if k != 'stop_after'}
                and original['id'] == fingerprint(original['view']),
                'delivery recovery must retain its original native job and exact authorization')
    run_id = operation['incremental']['run_id']
    round_id = operation['incremental']['scope_id']
    delivery = IncrementalDeliveryService(service.vault,
        admission=IncrementalControl(service.vault, run_id).check_admission,
        native_check=lambda current: require(current == own(), 'delivery native binding changed'))
    delivery_path = delivery._path(run_id, round_id)
    if delivery_path.is_file():
        prior = delivery.status(run_id, round_id)
        require(prior['authorization'] == view['authorization'], 'recovery changed the pinned delivery authorization')
        require(recovery is not None,
                'known delivery prefix needs an explicit mechanical recovery request')
        # Canonical bodies may already be the new revision. Recovery reads the
        # pinned Build/targets rather than rebuilding a new parent or draft.
        output = delivery.recover(run_id, round_id, binding, disposition=recovery['disposition'],
                                  synchronize=lambda rid, digest: synchronize_round_index(
                                      service.vault, root, rid, digest, view, own), stop_after=stop_after,
                                  isolation=recovery.get('isolation'))
        return {'state': 'settled', 'attempts': [], 'delivery': output, 'formal_run_id': prior['build_run_id']}
    require(recovery is None, 'delivery recovery cannot create a replacement Build for a missing prefix')
    pages, bodies = [], {}
    eligible = {row['resource_id']: row['eligible'] for row in service._source_eligibility(service._current_sets(), {})}
    for pointer in view['written']:
        preparation = read_pin(service.vault, pointer['preparation'])
        row = preparation['request']
        writer_root = _vault_path(service.vault, pointer['native_root'])
        result, _ = verified_result(writer_root, row)
        require(result['state'] == 'settled', 'incremental Build requires a settled native writing result')
        prepared = preparation['prepared']
        from short_evidence_pass import request as semantic_request
        expected = semantic_request(prepared['phase'], prepared['view'], prepared['schema'],
                                    row['budget']['estimated_output_tokens'], row['budget']['input_cap'],
                                    row['image_inputs'], reserve=row['budget']['input_reserve'])
        require(expected == row, 'page preparation differs from its exact native writing request')
        validate_preparation(service.build, prepared, operation['actor'])
        receipt = _load_json(writer_root / 'receipts' / (row['id'] + '.json'))
        content = validate_page_revision(prepared, receipt['answer'])
        change = prepared['view']['change']
        bodies[change['change_id']] = content['body']
        pages.append({'short_id': change['change_id'], 'name': change['name'], 'page_type': change['page_type'],
                      'card_path': change['path'], 'authored_sha256': content['authored_sha256'],
                      'support_refs': content['support_refs'], 'citation_bindings': content['citation_bindings'],
                      'incremental_change': change, 'native_fragments': [pointer], 'qa_issues': [],
                      'qa_status': 'usable', 'source_eligible_at_admission': all(
                          eligible.get(ref['unit_ref']['resource_id'], False) for ref in content['support_refs']),
                      'publication_authorized': True})
    bound_operation = {**operation, 'operation_sha256': runner.sha(root / 'operation.json')}
    changes_path = _vault_path(service.vault,
        f"_system/ledgers/incremental-ingest/{run_id}/rounds/{round_id}/page-changes.json")
    planned = {change['change_id']: change for change in _load_json(changes_path)}
    excluded = []
    for held in view['excluded']:
        change = planned[held['change_id']]
        require(change['page_id'] == held.get('page_id', change['page_id']),
                'held page disposition differs from the frozen page plan')
        excluded.append({'short_id': change['change_id'], 'name': change['name'],
                         'page_type': change['page_type'], 'card_path': change['path'],
                         'incremental_change': change, 'disposition': {'category': 'unresolved',
                             'reason': held.get('reason') or change['reason'],
                             'reopen_condition': 'new qualified evidence or an explicitly authorized affected revision'}})
    scope = build_scope(bound_operation, view['authorization'], pages, excluded)
    own()
    run = admit(service.build, scope, bodies, root, job)
    changes = {p['incremental_change']['page_id']: p['incremental_change'] for p in pages}
    reviews = [{'page_id': p['page_id'], 'authored_sha256': p['authored_sha256'],
                'parent_authored_sha256': changes[p['page_id']]['parent_authored_sha256'], 'actor': run['actor'],
                'acceptance': record(assess(service.build, run, p)),
                'note': 'Verified native incremental writing and original evidence.'} for p in run['page_revisions']]
    own()
    base = view['authorization']['base_release']
    base_hash = runner.sha(service._manifest_path(base)) if base else None
    delivery.prepare({'operation_id': 'prepare-' + view['delivery_id'], 'actor': run['actor'],
                      'run_id': operation['incremental']['run_id'], 'round_id': operation['incremental']['scope_id'],
                      'authorization': view['authorization'], 'build_run_id': run['run_id'],
                      'finalize_request': {'run_id': run['run_id'], 'actor': run['actor'],
                                           'expected_revision': run['revision'], 'reviews': reviews},
                      'base_release': base, 'base_release_sha256': base_hash,
                      **({key: view['authorization'][key] for key in ('source_changes', 'authorized_removed_pages')}
                         if view['authorization'].get('source_changes') else {})}, own())
    output = delivery.deliver(run_id, round_id, own(),
        synchronize=lambda rid, digest: synchronize_round_index(service.vault, root, rid, digest, view, own),
        stop_after=stop_after)
    return {'state': 'settled', 'attempts': [], 'delivery': output, 'formal_run_id': run['run_id']}


def synchronize_round_index(vault, root, release_id, release_hash, view, own):
    """Call the sole Skill index writer for one pinned round release."""
    import os
    import subprocess
    import sys
    from hermes_source_units.incremental_delivery import require_index_admission

    own()
    require_index_admission(vault, release_id, release_hash)
    saved = root / 'provider-result.json'
    if saved.is_file():
        index = _load_json(saved)['index']
        require(index.get('status') == 'ready' and index.get('release_id') == release_id
                and index.get('release_hash') == release_hash and index.get('index_generation'),
                'saved Provider result differs from the exact round release')
        return index
    finalize = Path(__file__).resolve().parents[2] / 'hermes-obsidian-knowledge-finalize'
    provider = Path(view['provider_config']).resolve(strict=True)
    config_hash = view.get('provider_config_sha256')
    require(isinstance(config_hash, str) and len(config_hash) == 64 and _sha(provider.read_bytes()) == config_hash,
            'Provider configuration changed after round authorization')
    library = finalize / 'lib'
    environment = {**os.environ, 'PYTHONPATH': str(library), 'PYTHONDONTWRITEBYTECODE': '1'}
    process = subprocess.run([sys.executable, '-B', str(finalize / 'scripts/sync_release_index.py'),
                              str(vault), '--provider-config', str(provider)],
                             capture_output=True, text=True, encoding='utf-8', env=environment, timeout=1900)
    own()
    result = json.loads(process.stdout)
    index = result['index']
    require(process.returncode == 0 and index.get('status') == 'ready' and index.get('release_id') == release_id
            and index.get('release_hash') == release_hash and index.get('index_generation'),
            'Provider failed to synchronize the exact incremental release')
    _write_atomic(saved, _json_bytes({'returncode': process.returncode, 'index': index}))
    return index
