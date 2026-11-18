"""Source-parallel, finite-round incremental ingest using native Hermes workers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_ingest import CONTRACTS, FileIncrementalIngestService
from hermes_source_units.incremental_sources import pinned, read_pin, safe_id
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _sha, _vault_path, _write_atomic
from hermes_source_units.validation import ContractError, fingerprint
from incremental_native import mutate, service, verified_result
from short_evidence_pass import load_sources, require, request as semantic_request, seed_requests, mapping_groups, MAP_CONTRACT


def request_path(domain, run_id):
    return domain.root(run_id) / 'runtime-request.json'


def initialize_scope(domain, run_id, kind, scope_id, stage, snapshots, *, recovery_id=None):
    import run_short_evidence_test as native
    from prepare_short_evidence_test import save
    from orchestration import configuration

    directory = domain.root(run_id) / 'native' / kind / scope_id / stage
    if recovery_id:
        directory = directory / 'recoveries' / safe_id(recovery_id)
    path = directory / 'operation.json'
    if path.is_file():
        operation = _load_json(path)
        require(operation['incremental']['snapshots'] == snapshots, 'incremental workspace inputs changed')
        return directory, operation
    run = domain.status(run_id)
    cfg = configuration()
    operation = {'contract': native.CONTRACT, 'workflow_mode': 'incremental_v1', 'actor': run['actor'],
                 'vault': str(domain.vault), 'board': 'incremental-' + fingerprint([run_id, kind, scope_id, stage, recovery_id])[:32],
                 'origin': {'workflow_id': run['source_workflow_id'], 'snapshots': snapshots},
                 'incremental': {'run_id': run_id, 'scope_kind': kind, 'scope_id': scope_id, 'stage': stage,
                                 'snapshots': snapshots, 'input_fingerprint': fingerprint([snapshots, scope_id])},
                 'pass_capacity': cfg['pass_worker_concurrency'], 'page_capacity': cfg['reduce_page_concurrency']}
    kb, _, _ = native.runtime()
    if not any(board['slug'] == operation['board'] for board in kb.list_boards()):
        kb.create_board(operation['board'], name='Incremental ' + scope_id, default_workdir=str(directory))
    save(path, operation)
    return directory, operation


def execute_rows(domain, run_id, root, operation, rows):
    import run_short_evidence_test as native

    waiting = []
    for row in rows:
        job = root / 'jobs' / (row['id'] + '.json')
        if job.with_suffix('.result.json').is_file():
            verified_result(root, row)
        else:
            require(not list(job.parent.glob(job.stem + '.attempt-*.json')),
                    'consumed native request has unknown completion; supported recovery is required')
            waiting.append(row)
    if waiting:
        native.stage(root, operation, waiting, operation['page_capacity']
                     if waiting[0]['phase'] == 'incremental-page-write' else operation['pass_capacity'])
    results = []
    for row in rows:
        result, proof = verified_result(root, row)
        results.append((row, result, proof))
        settle_usage(domain, run_id, root, row, result)
    return results


def settle_usage(domain, run_id, root, row, result):
    from hermes_state import SessionDB
    from workflow_metrics import usage_for

    database = SessionDB(read_only=True)
    path = database.db_path
    database.close()
    for attempt in result['attempts']:
        key = fingerprint({'request': row['id'], 'ordinal': attempt['ordinal']})
        budget_id, reservation = next((k, v) for k, v in domain.status(run_id)['reservations'].items()
                                      if v['request_fingerprint'] == key)
        if reservation['state'] == 'settled':
            continue
        usage = usage_for([attempt], path)
        if not attempt.get('model') or usage['unknown_samples']['input'] or usage['unknown_samples']['output']:
            continue
        actual = {'calls': 1, 'input_tokens': int(usage['known_totals']['input']),
                  'output_tokens': int(usage['known_totals']['output'])}
        record = {'request_fingerprint': key, 'actual': actual, 'native_usage': usage}
        ref = root / 'usage' / (key + '.json')
        _write_atomic(ref, _json_bytes(record))
        mutate(domain, run_id, 'settle_budget', 'usage-' + key[:64], budget_id=budget_id, actual=actual,
               usage=pinned(domain.vault, ref.relative_to(domain.vault).as_posix()))


def source_pin(snapshot):
    require(isinstance(snapshot.get('bundle'), dict),
            'incremental short evidence mapping requires a verified governed Bundle')
    return {'original': snapshot['original']['ref'], 'resource_id': snapshot['identity']['resource_id'],
            'unit_set_id': snapshot['unit_set']['id'], 'bundle_manifest': snapshot['bundle']['manifest_ref']}


def source_alias(run, snapshot):
    return 'S' + str(next(i for i, row in enumerate(run['source_manifest'], 1)
                         if row['path'] == snapshot['original']['ref']))


def map_source(vault, run_id, snapshot_id):
    import run_short_evidence_test as native
    from prepare_short_evidence_test import advance, save
    from short_page_contract import CONTRACT as PAGE_CONTRACT

    domain = service(Path(vault), run_id)
    snapshot = domain.validate_source_snapshot(run_id, snapshot_id)
    control = IncrementalControl(domain.vault, run_id)
    scope = {'scope_kind': 'source', 'scope_id': snapshot_id}
    with control.executing(scope, 'mapping', fingerprint(snapshot)):
        root, operation = initialize_scope(domain, run_id, 'source', snapshot_id, 'mapping', [snapshot_id])
        qualify(domain, run_id, snapshot_id)
        alias = source_alias(domain.status(run_id), snapshot)
        sources = load_sources(domain.vault, [source_pin(snapshot)], aliases={snapshot['identity']['resource_id']: alias},
                               actor=domain.status(run_id)['actor'])
        for source in sources.values():
            source['incremental_snapshot_id'] = snapshot_id
        seeds, groups = seed_requests(sources), mapping_groups(sources, MAP_CONTRACT)
        holds = {}
        while True:
            plan = advance(sources, seeds, groups, root / 'receipts', native.LIMIT,
                           native_holds=holds, mapping_contract=MAP_CONTRACT, page_contract=PAGE_CONTRACT)
            if plan['stage'] not in ('topic-seed', 'map', 'local-map'):
                require('reduced' in plan, 'source Map stopped without a coverage result')
                break
            results = execute_rows(domain, run_id, root, operation, plan['requests'])
            for row, result, proof in results:
                if result['state'] == 'held':
                    holds[row['id']] = {'reason': 'native request held', 'request_id': row['id']}
        # Reconstruct complete producer lineage even when advance() reuses all
        # old receipts and no longer emits their already completed requests.
        proofs = []
        for path in sorted((root / 'jobs').glob('*.json')):
            if len(path.stem) != 64 or any(character not in '0123456789abcdef' for character in path.stem):
                continue
            row = _load_json(path)['request']
            require(row['phase'] in ('short-topics', 'short-map'), 'unexpected job in source mapping scope')
            require(path.with_suffix('.result.json').is_file(), 'source mapping native completion remains unknown')
            row, result, proof = execute_rows(domain, run_id, root, operation, [row])[0]
            proofs.append({'proof': proof, 'task_body': json.loads(native.task_body(root, path))})
        reduced_path = root / 'mapped.json'
        save(reduced_path, plan['reduced'])
        require(proofs, 'mapped result requires actual native completion')
        aliases = {key: {'unit_ref': item['source_ref']['unit_ref'], 'sha256': item['sha256'],
                         'snapshot_id': snapshot_id} for source in sources.values() for key, item in source['items'].items()}
        coverage = [{'unit_ref': item['source_ref']['unit_ref'],
                     'disposition': {'linked': 'mapped', 'excluded': 'excluded'}.get(
                         plan['reduced']['dispositions'][key]['status'], 'held'),
                     'reason': plan['reduced']['dispositions'][key].get('reason', 'source Map coverage disposition')}
                    for source in sources.values() for key, item in source['items'].items()]
        summary = fingerprint({'snapshot': snapshot_id, 'reduced': native.sha(reduced_path)})
        budget = mutate(domain, run_id, 'reserve_budget', 'map-budget-' + summary[:48], reservation={
            'request_fingerprint': summary, 'phase': 'mapping', 'max_calls': 0, 'max_input_tokens': 0, 'max_output_tokens': 0})
        usage_path = root / 'usage' / 'map-summary.json'
        _write_atomic(usage_path, _json_bytes({'request_fingerprint': summary,
                                             'actual': {'calls': 0, 'input_tokens': 0, 'output_tokens': 0}}))
        mutate(domain, run_id, 'settle_budget', 'map-summary-' + summary[:48], budget_id=budget['budget_id'],
               actual={'calls': 0, 'input_tokens': 0, 'output_tokens': 0}, usage=pinned(domain.vault, usage_path.relative_to(domain.vault).as_posix()))
        return mutate(domain, run_id, 'commit_source_map', 'map-' + snapshot_id[-56:], snapshot_id=snapshot_id,
            budget_id=budget['budget_id'], request_fingerprint=summary, native_completion=proofs[-1]['proof'],
            task_body=proofs[-1]['task_body'], native_completions=proofs, alias_table=aliases, coverage=coverage,
            results=[pinned(domain.vault, reduced_path.relative_to(domain.vault).as_posix()),
                     *[pinned(domain.vault, p.relative_to(domain.vault).as_posix()) for p in sorted((root / 'receipts').glob('*.json'))]],
            unresolved=plan['reduced'].get('unresolved', []))


def qualify(domain, run_id, snapshot_id):
    """Use the deterministic source binding rules under the incremental seal."""
    scripts = Path(__file__).resolve().parents[2] / 'hermes-obsidian-controlled-ingest/scripts'
    sys.path.insert(0, str(scripts))
    from governance_repository import GovernanceError, JsonGovernanceRepository
    from source_governance_resolution import digest
    from prepare_short_evidence_test import save

    snapshot = domain.validate_source_snapshot(run_id, snapshot_id)
    run = domain.status(run_id)
    repo = JsonGovernanceRepository(domain.vault)
    wid = run['source_workflow_id']
    operation_id = 'incremental-source-' + fingerprint([run_id, snapshot_id])[:48]
    transaction = domain.vault / '_system/metadata/source-governance-resolutions' / operation_id / 'transaction.json'
    root = domain.root(run_id) / 'source-qualification' / snapshot_id
    # Serialize qualifiers across runs. A source worker can still register its
    # own result between preview and apply; retry one uncommitted revision race.
    with _exclusive_lock(_vault_path(domain.vault, '_system/metadata/.incremental-source-qualification.lock')):
        if transaction.is_file():
            resolution = _load_json(transaction)['request']
            require(resolution['incremental_run_id'] == run_id and resolution['snapshot_id'] == snapshot_id,
                    'saved qualification belongs to another incremental source')
            result = repo.resolve_sources(resolution)
            save(root / 'receipt.json', {key: value for key, value in result.items() if key != 'replayed'})
            return result
        for attempt in (1, 2):
            organization, registry = repo.load_state()
            workflow = _load_json(_vault_path(domain.vault, f'_system/ledgers/ingest-workflows/{wid}.json'))
            resolution = {'workflow_id': wid, 'workflow_ref': f'_system/ledgers/ingest-workflows/{wid}.json',
                          'actor': run['actor'], 'operation_id': operation_id,
                          'reason': 'Explicit incremental source qualification using verified native seal and declarations.',
                          'source_boundary': 'incremental-source/v1', 'incremental_run_id': run_id, 'snapshot_id': snapshot_id,
                          'automatic_approval_activation': True, 'expected_workflow_revision': workflow['revision'],
                          'expected_organization_revision': organization['registry_revision'],
                          'expected_registry_revision': registry['registry_revision'],
                          'resource_ids': [snapshot['identity']['resource_id']]}
            resolution['input_digest'] = digest(resolution)
            try:
                preview = repo.resolve_sources(resolution, dry_run=True)
                require(preview['resolved'], 'source identity has no deterministic qualification')
                resolution['preview_digest'] = preview['preview_digest']
                resolution['input_digest'] = digest({k: v for k, v in resolution.items() if k != 'input_digest'})
                save(root / ('request-' + fingerprint(resolution) + '.json'), resolution)
                result = repo.resolve_sources(resolution)
                save(root / 'receipt.json', result)
                return result
            except GovernanceError as error:
                if attempt != 1 or transaction.exists() or 'Governance revision conflict' not in str(error):
                    raise
        raise AssertionError('bounded qualification attempts exhausted')


def start(request, record, bootstrap_skill):
    from start_vault_workflow import start as provision
    from incremental_ingest_controller import spawn

    require(request.get('workflow_mode') == 'incremental_v1' and not request.get('test_mode'),
            'incremental product mode must be explicit and cannot use historical test import')
    require('incremental_budget' in request and 'incremental_policy' in request,
            'incremental start requires explicit cumulative budgets and fixed round bounds')
    run_id = safe_id(request.get('run_id', request['operation_id']))
    vault = Path(request['vault_path']).resolve()
    existing = vault / '_system/ledgers/incremental-ingest' / run_id / 'runtime-request.json'
    if existing.is_file():
        previous = _load_json(existing)
        require(previous['source_request'] == {**request, 'incremental_run_id': run_id}, 'incremental start inputs changed')
        return service(vault, run_id).status(run_id)
    source_request = {**request, 'incremental_run_id': run_id}
    provision(source_request, record, bootstrap_skill, prepare_only=True, incremental_bootstrap=True)
    require(type(request.get('incremental_manual_sources', False)) is bool,
            'manual source admission must be an explicit boolean')
    manifest = [{'path': request.get('incremental_reuse_originals', {}).get(str(Path(p).resolve()),
                       f"10_Raw/Incremental/{request['operation_id']}/" + Path(p).name),
                 'sha256': _sha(Path(p).resolve(strict=True).read_bytes())}
                for p in request['sources']]
    control = IncrementalControl(vault, run_id)
    control.initialize(request['actor'], request.get('incremental_pause_after', []),
                       manual_sources=manifest if request.get('incremental_manual_sources') else None)
    domain = service(vault, run_id)
    initial = domain.start({'workflow_mode': 'incremental_v1', 'run_id': run_id,
        'operation_id': request['operation_id'], 'actor': request['actor'], 'sources': manifest,
        'source_workflow_id': 'ingest-' + request['operation_id'], 'contracts': CONTRACTS,
        'policy': request['incremental_policy'], 'budget': request['incremental_budget'],
        'control_ref': control.path.relative_to(vault).as_posix()})
    from prepare_short_evidence_test import save
    save(request_path(domain, run_id), {'source_request': source_request, 'record': str(Path(record).resolve()),
                                      'bootstrap_skill': str(Path(bootstrap_skill).resolve()),
                                      'provider_config': request['incremental_provider_config']})
    if control.checkpoint({'scope_kind': 'run', 'scope_id': run_id}, 'prepare', manifest)['state'] != 'active':
        return initial
    provision(source_request, record, bootstrap_skill, incremental_bootstrap=True)
    spawn(vault, run_id)
    return domain.status(run_id)
