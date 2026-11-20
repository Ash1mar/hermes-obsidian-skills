"""Vault-owned incremental run, source, budget and finite-round records."""
from __future__ import annotations

import copy

from .incremental_qualification import BACKOFF_SECONDS, LOCK_CONTENTION_CODES, WAIT_CONTRACT, qualification_id
from .incremental_sources import (pinned, read_pin, require, safe_id, snapshot_content,
                                  verify_native_completion, verify_native_drain)
from .source_units import _exclusive_lock, _json_bytes, _load_json, _sha, _vault_path, _write_atomic
from .validation import ContractError, fingerprint


ROOT = '_system/ledgers/incremental-ingest'
REPORTS = '_system/reports/incremental-ingest'
MODE = 'incremental_v1'
CONTRACTS = {'run': 'incremental-run/v1', 'snapshot': 'incremental-source-snapshot/v1',
             'seal': 'incremental-source-seal/v1', 'map': 'incremental-map-result/v1',
             'round': 'incremental-round-input/v1', 'control': 'incremental-control/v1'}


class FileIncrementalIngestService:
    """Admission and native verifiers are supplied by the trusted runtime adapter.

    File records establish content identity. The adapter independently checks the
    current controller/worker grant on every mutation and reads ended native runs
    for historical outputs. Neither file existence nor a supplied success flag
    constitutes execution authority.
    """

    def __init__(self, vault, *, check_admission=None, native_evidence=None):
        from pathlib import Path

        self.vault = Path(vault).resolve(strict=True)
        self.vault_id = _load_json(_vault_path(self.vault, '_system/vault.json'))['vault']['id']
        self.check_admission = check_admission
        self.native_evidence = native_evidence or verify_native_completion

    def root(self, run_id):
        return _vault_path(self.vault, f'{ROOT}/{safe_id(run_id)}')

    def reports(self, run_id):
        return _vault_path(self.vault, f'{REPORTS}/{safe_id(run_id)}')

    def _lock(self):
        return _exclusive_lock(_vault_path(self.vault, f'{ROOT}/.coordinator.lock'))

    def status(self, run_id):
        value = _load_json(self.root(run_id) / 'run.json')
        require(value['contract'] == CONTRACTS['run'] and value['workflow_mode'] == MODE
                and value['run_id'] == run_id and value['vault_id'] == self.vault_id,
                'incremental run identity differs')
        return value

    def _admit(self, run, stage, scope_kind, scope_id, inputs):
        require(callable(self.check_admission), 'incremental control admission is unavailable', 'ACCESS_DENIED')
        scope = {'run_id': run['run_id'], 'actor': run['actor'], 'scope_kind': scope_kind,
                 'scope_id': scope_id, 'stage': stage, 'input_fingerprint': fingerprint(inputs)}
        grant = self.check_admission(scope)
        require(isinstance(grant, dict) and grant.get('allowed') is True
                and grant.get('input_fingerprint') == scope['input_fingerprint'],
                'incremental control did not admit the exact inputs', 'ACCESS_DENIED')
        return grant

    def _immutable(self, path, value):
        if path.is_file():
            require(_load_json(path) == value, 'immutable incremental result identity changed', 'IDEMPOTENCY_CONFLICT')
        else:
            _write_atomic(path, _json_bytes(value))
        return pinned(self.vault, path.relative_to(self.vault).as_posix())

    def _apply_prepared(self, run_id):
        """Finish an operation index write after its immutable evidence was saved."""
        path = self.root(run_id) / 'run.json'
        for operation in sorted((self.root(run_id) / 'operations').glob('*.json')):
            journal = _load_json(operation)
            if journal.get('state') != 'prepared':
                continue
            current = _load_json(path)
            proposed = journal['run_after']
            require(current == proposed or fingerprint(current) == journal['run_before_fingerprint'],
                    'unfinished operation no longer matches run revision', 'REVISION_CONFLICT')
            for pointer in journal['evidence']:
                read_pin(self.vault, pointer)
            _write_atomic(path, _json_bytes(proposed))
            _write_atomic(operation, _json_bytes({**journal, 'state': 'committed'}))

    def _mutate(self, request, stage, kind, scope_id, update):
        run_id, operation_id = safe_id(request['run_id']), safe_id(request['operation_id'])
        with self._lock():
            self._apply_prepared(run_id)
            before = self.status(run_id)
            require(request['actor'] == before['actor'], 'run actor differs', 'ACCESS_DENIED')
            journal = self.root(run_id) / 'operations' / f'{operation_id}.json'
            request_hash = fingerprint(request)
            if journal.exists():
                existing = _load_json(journal)
                require(existing['request_fingerprint'] == request_hash, 'operation key identifies different inputs',
                        'IDEMPOTENCY_CONFLICT')
                return existing['result']
            require(type(request['expected_revision']) is int and request['expected_revision'] == before['revision'],
                    'incremental run revision changed', 'REVISION_CONFLICT')
            require(before['state'] == 'active', 'incremental run has closed', 'INVALID_TRANSITION')
            grant = self._admit(before, stage, kind, scope_id, request)
            after = copy.deepcopy(before)
            result, evidence = update(after, grant)
            after['revision'] += 1
            after['last_operation_id'] = operation_id
            transaction = {'contract': 'incremental-operation/v1', 'operation_id': operation_id,
                           'request_fingerprint': request_hash, 'expected_revision': before['revision'],
                           'run_before_fingerprint': fingerprint(before),
                           'run_after': after, 'result': result, 'evidence': evidence, 'state': 'prepared'}
            _write_atomic(journal, _json_bytes(transaction))
            _write_atomic(self.root(run_id) / 'run.json', _json_bytes(after))
            _write_atomic(journal, _json_bytes({**transaction, 'state': 'committed'}))
            return result

    def start(self, request):
        run_id = safe_id(request['run_id'])
        operation_id = safe_id(request['operation_id'])
        require(request.get('workflow_mode') == MODE and request.get('actor'), 'explicit incremental mode and actor required')
        require(request.get('contracts') == CONTRACTS, 'incremental contract set must be pinned')
        require(isinstance(request.get('policy'), dict) and isinstance(request.get('control_ref'), str),
                'fixed policy and control protocol reference required')
        require(type(request['policy'].get('max_maps_per_round')) is int and request['policy']['max_maps_per_round'] > 0,
                'positive deterministic round bound required')
        require(request.get('source_workflow_id') and request.get('sources'), 'fixed source workflow and source manifest required')
        safe_id(request['source_workflow_id'])
        sources = []
        for item in request['sources']:
            path = _vault_path(self.vault, item['path'])
            require(path.is_file() and _sha(path.read_bytes()) == item['sha256'], 'fixed source bytes differ', 'SOURCE_CHANGED')
            require(set(item) == {'path', 'sha256'}, 'source manifest only accepts path and sha256')
            sources.append(dict(item))
        require(len({row['path'] for row in sources}) == len(sources), 'duplicate source scope')
        budgets = request['budget']
        require(set(budgets) == {'max_calls', 'max_input_tokens', 'max_output_tokens', 'phase_limits'}
                and all(type(budgets[k]) is int and budgets[k] >= 0 for k in
                        ('max_calls', 'max_input_tokens', 'max_output_tokens'))
                and isinstance(budgets['phase_limits'], dict)
                and all(type(value) is int and value >= 0 for value in budgets['phase_limits'].values()),
                'explicit cumulative and per-phase budgets required')
        initial = {'contract': CONTRACTS['run'], 'run_id': run_id, 'vault_id': self.vault_id,
                   'actor': request['actor'], 'workflow_mode': MODE, 'revision': 1, 'state': 'active',
                   'source_workflow_id': request['source_workflow_id'], 'source_manifest': sources,
                   'policy': request['policy'], 'contracts': request['contracts'], 'control_ref': request['control_ref'],
                   'budget': budgets, 'reservations': {}, 'sources': {row['path']: {'state': 'pending'} for row in sources},
                   'map_result_ids': [], 'consumed_map_ids': [], 'round_ids': [], 'active_round_id': None,
                   'start_fingerprint': fingerprint(request), 'last_operation_id': operation_id}
        with self._lock():
            path = self.root(run_id) / 'run.json'
            if path.exists():
                existing = self.status(run_id)
                require(existing['start_fingerprint'] == initial['start_fingerprint'], 'run key identifies different scope',
                        'IDEMPOTENCY_CONFLICT')
                return existing
            self._admit(initial, 'start', 'run', run_id, request)
            self._immutable(self.root(run_id) / 'start-request.json', request)
            _write_atomic(path, _json_bytes(initial))
            return initial

    def seal_source_snapshot(self, request):
        path = request['source_path']

        def update(run, grant):
            original = next((row for row in run['source_manifest'] if row['path'] == path), None)
            require(original is not None, 'source is outside fixed run scope', 'ACCESS_DENIED')
            workflow = _load_json(_vault_path(self.vault, f"_system/ledgers/ingest-workflows/{run['source_workflow_id']}.json"))
            outcome = request['outcome']
            require(outcome in workflow['source_outcomes'], 'source outcome is not durable in its workflow')
            completion = self.native_evidence(self.vault, request['native_completion'], {
                'source_workflow_id': run['source_workflow_id'], 'source_path': path, 'source_sha256': original['sha256']})
            require(workflow['actor'] == run['actor'] and not workflow['cancel_requested'], 'source workflow stopped or actor changed')
            snapshot, registry = snapshot_content(self.vault, run['actor'], original, outcome)
            destination = self.reports(run['run_id']) / 'source-snapshots' / (snapshot['snapshot_id'] + '.json')
            pointer = self._immutable(destination, snapshot)
            seal = {'contract': CONTRACTS['seal'], 'run_id': run['run_id'], 'snapshot': pointer,
                    'snapshot_id': snapshot['snapshot_id'], 'operation_id': request['operation_id'],
                    'producer': completion, 'native_completion': request['native_completion'],
                    'sealer_authorization': grant, 'registry_revision': registry,
                    'verified': True}
            seal['seal_id'] = 'seal-' + fingerprint(seal)
            seal_pin = self._immutable(self.reports(run['run_id']) / 'source-seals' / (seal['seal_id'] + '.json'), seal)
            row = run['sources'][path]
            require(row['state'] in ('pending', 'prepared', 'mapped') and row.get('snapshot_id') in (None, snapshot['snapshot_id']),
                    'source snapshot changed or mapping already started')
            run['sources'][path] = {**row, 'state': 'mapped' if row['state'] == 'mapped' else 'prepared',
                                     'snapshot_id': snapshot['snapshot_id'], 'snapshot': pointer,
                                     'seal': seal_pin, 'source_outcome': outcome}
            return {'snapshot': snapshot, 'seal': seal}, [pointer, seal_pin]

        return self._mutate(request, 'source-seal', 'source', path, update)

    def validate_source_snapshot(self, run_id, snapshot_id, purpose='construction'):
        run = self.status(run_id)
        row = next((row for row in run['sources'].values() if row.get('snapshot_id') == snapshot_id), None)
        require(row is not None, 'snapshot is not in this run', 'ACCESS_DENIED')
        seal = read_pin(self.vault, row['seal'])
        stored = read_pin(self.vault, row['snapshot'])
        require(seal['snapshot'] == row['snapshot'] and seal['snapshot_id'] == snapshot_id, 'source seal differs')
        original = {'path': stored['original']['ref'], 'sha256': stored['original']['sha256']}
        self.native_evidence(self.vault, seal['native_completion'], {
            'source_workflow_id': run['source_workflow_id'], 'source_path': original['path'],
            'source_sha256': original['sha256']})
        outcome = dict(row['source_outcome'])
        if stored['bundle']:
            outcome['artifact_refs'] = [stored['bundle']['manifest_ref']]
        current, _ = snapshot_content(self.vault, run['actor'], original, outcome)
        require(current == stored, 'source snapshot content changed', 'SOURCE_CHANGED')
        if purpose == 'query':
            from .source_units import FileSourceUnitService

            source = FileSourceUnitService(self.vault)
            registry = _load_json(_vault_path(self.vault, '_system/metadata/document-registry.json'))['registry_revision']
            source.get({'source_ref': {'unit_ref': stored['units'][0]['unit_ref'], 'span': None},
                        'access': {'actor': run['actor'], 'purpose': purpose, 'registry_revision': registry}})
        else:
            require(purpose == 'construction', 'unsupported source snapshot purpose')
        return stored

    def record_source_failure(self, request):
        def update(run, grant):
            path, outcome = request['source_path'], request['outcome']
            require(path in run['sources'], 'source is outside fixed scope', 'ACCESS_DENIED')
            workflow = _load_json(_vault_path(self.vault, f"_system/ledgers/ingest-workflows/{run['source_workflow_id']}.json"))
            require(outcome in workflow['source_outcomes'] and outcome['status'] == 'failed' and outcome['path'] == path,
                    'a durable typed source failure is required')
            from .ingest_workflow import SOURCE_FAILURE_CODES

            require(outcome['error_code'] in SOURCE_FAILURE_CODES and run['sources'][path]['state'] == 'pending',
                    'runtime errors cannot become source failures')
            run['sources'][path] = {'state': 'failed', 'outcome': outcome}
            return {'source_path': path, 'state': 'failed'}, []

        return self._mutate(request, 'source-seal', 'source', request['source_path'], update)

    def hold_source_mapping(self, request):
        def update(run, grant):
            path, snapshot_id = request['source_path'], request['snapshot_id']
            source = run['sources'].get(path, {})
            require(source.get('state') == 'prepared' and source.get('snapshot_id') == snapshot_id,
                    'qualification hold must name this exact prepared source')
            self.validate_source_snapshot(run['run_id'], snapshot_id)
            pointer = request['wait']
            expected = self.root(run['run_id']) / 'source-qualification' / safe_id(snapshot_id) / 'wait.json'
            require(pointer['ref'] == expected.relative_to(self.vault).as_posix(), 'qualification wait evidence is outside its snapshot')
            wait = read_pin(self.vault, pointer)
            workflow_id = run['source_workflow_id']
            locks = {f'_system/ledgers/ingest-workflows/{folder}/{workflow_id}.lock'
                     for folder in ('.dispatch-locks', '.locks')}
            locks.add('_system/metadata/.incremental-source-qualification.lock')
            attempts = wait.get('attempts', [])
            require(wait.get('contract') == WAIT_CONTRACT and wait.get('run_id') == run['run_id']
                    and wait.get('snapshot_id') == snapshot_id and wait.get('state') == 'exhausted'
                    and wait.get('qualification_id') == qualification_id(run['run_id'], snapshot_id)
                    and wait.get('retry_at') is None and len(attempts) == len(BACKOFF_SECONDS) + 1
                    and all(a['attempt'] == i and a['lock'] in locks and a['error_code'] in LOCK_CONTENTION_CODES
                            and type(a['wait_ms']) is int and a['wait_ms'] >= 0 for i, a in enumerate(attempts, 1)),
                    'source qualification retry allowance is not evidenced as exhausted')
            hold = {'state': 'held', 'stage': 'mapping', 'source_path': path, 'snapshot_id': snapshot_id,
                    'reason': 'source qualification lock retry allowance exhausted', 'wait': pointer}
            run['sources'][path] = {**source, 'state': 'mapping_held', 'mapping_hold': hold}
            return hold, [pointer]

        return self._mutate(request, 'mapping', 'source', request['snapshot_id'], update)

    def reserve_budget(self, request):
        def update(run, grant):
            reservation = request['reservation']
            require(set(reservation) == {'request_fingerprint', 'phase', 'max_calls', 'max_input_tokens', 'max_output_tokens'}
                    and all(type(reservation[k]) is int and reservation[k] >= 0 for k in
                            ('max_calls', 'max_input_tokens', 'max_output_tokens')), 'invalid semantic reservation')
            key = fingerprint(reservation)
            if key in run['reservations']:
                return {'budget_id': key, **run['reservations'][key]}, []
            require(not any(row['request_fingerprint'] == reservation['request_fingerprint']
                            for row in run['reservations'].values()), 'semantic request already owns another reservation')
            for maximum in ('max_calls', 'max_input_tokens', 'max_output_tokens'):
                require(sum(row[maximum] for row in run['reservations'].values()) + reservation[maximum] <= run['budget'][maximum],
                        'cumulative budget exhausted', 'BUDGET_EXHAUSTED')
            phase = reservation['phase']
            require(phase in run['budget']['phase_limits'] and
                    sum(row['max_calls'] for row in run['reservations'].values() if row['phase'] == phase)
                    + reservation['max_calls'] <= run['budget']['phase_limits'][phase],
                    'phase call budget exhausted', 'BUDGET_EXHAUSTED')
            # Exact unchanged semantic work cannot obtain a fresh call budget by
            # creating another run. Reuse its verified result or keep unknown
            # consumption unresolved under the original operation.
            claim_path = _vault_path(self.vault,
                '_system/metadata/incremental-work-budgets/' + fingerprint(reservation['request_fingerprint']) + '.json')
            claim = {'contract': 'incremental-work-budget-owner/v1', 'run_id': run['run_id'],
                     'request_fingerprint': reservation['request_fingerprint'], 'budget_id': key}
            if claim_path.exists():
                require(_load_json(claim_path) == claim, 'unchanged work already owns a budget in another run',
                        'SEMANTIC_REUSE_REQUIRED')
            claim_pin = self._immutable(claim_path, claim)
            run['reservations'][key] = {**reservation, 'state': 'reserved', 'actual': None}
            return {'budget_id': key, **run['reservations'][key]}, [claim_pin]

        return self._mutate(request, 'reserve', request.get('scope_kind', 'run'), request.get('scope_id', request['run_id']), update)

    def settle_budget(self, request):
        def update(run, grant):
            row = run['reservations'][request['budget_id']]
            actual = request['actual']
            require(row['state'] in ('reserved', 'settled') and set(actual) == {'calls', 'input_tokens', 'output_tokens'}
                    and all(type(value) is int and 0 <= value <= row['max_' + key] for key, value in actual.items()),
                    'actual usage exceeds its original reservation', 'BUDGET_EXHAUSTED')
            require(row['actual'] in (None, actual), 'budget settlement changed', 'IDEMPOTENCY_CONFLICT')
            usage_pointer = request['usage']
            usage = read_pin(self.vault, usage_pointer)
            require(usage.get('request_fingerprint') == row['request_fingerprint'] and usage.get('actual') == actual,
                    'actual usage must bind saved native usage evidence')
            row.update(state='settled', actual=actual, usage=usage_pointer)
            return {'budget_id': request['budget_id'], 'actual': actual}, [usage_pointer]

        return self._mutate(request, 'settle-budget', 'run', request['run_id'], update)

    def commit_source_map(self, request):
        def update(run, grant):
            snapshot = self.validate_source_snapshot(run['run_id'], request['snapshot_id'])
            budget_id = request['budget_id']
            budget = run['reservations'].get(budget_id)
            require(budget and budget['request_fingerprint'] == request['request_fingerprint'],
                    'mapping result has no original cumulative reservation')
            proofs = request.get('native_completions') or [{'proof': request['native_completion'],
                                                          'task_body': request['task_body']}]
            require(proofs, 'all mapped native completions must be provided')
            completions = [{**self.native_evidence(self.vault, row['proof'], {
                'task_body': row['task_body'], 'run_id': run['run_id'], 'snapshot_id': snapshot['snapshot_id']}),
                'proof': row['proof'], 'task_body': row['task_body']} for row in proofs]
            aliases = request['alias_table']
            expected_units = {fingerprint(row['unit_ref']): row['sha256'] for row in snapshot['units']}
            require(aliases and all(row['snapshot_id'] == snapshot['snapshot_id']
                                    and expected_units.get(fingerprint(row['unit_ref'])) == row['sha256']
                                    for row in aliases.values()), 'request aliases differ from the source snapshot')
            coverage = request['coverage']
            require(len(coverage) == len(expected_units)
                    and {fingerprint(row['unit_ref']) for row in coverage} == set(expected_units)
                    and all(row['disposition'] in ('mapped', 'held', 'excluded') and
                            (row['disposition'] == 'mapped' or row.get('reason')) for row in coverage),
                    'all source Units require an explicit coverage disposition')
            results = request['results']
            require(results, 'native mapping results required')
            for pointer in results:
                read_pin(self.vault, pointer)
            record = {'contract': CONTRACTS['map'], 'snapshot_id': snapshot['snapshot_id'],
                      'request_fingerprint': request['request_fingerprint'], 'alias_table': aliases,
                      'results': results, 'coverage': coverage, 'unresolved': request.get('unresolved', []),
                      'native_completions': completions, 'budget_id': budget_id}
            record['map_result_id'] = 'map-' + fingerprint(record)
            pointer = self._immutable(self.reports(run['run_id']) / 'map-results' / (record['map_result_id'] + '.json'), record)
            if record['map_result_id'] not in run['map_result_ids']:
                source = run['sources'][snapshot['original']['ref']]
                require(source['state'] == 'prepared', 'source already has another mapped result', 'IDEMPOTENCY_CONFLICT')
                source.update(state='mapped', map_result_id=record['map_result_id'])
                run['map_result_ids'].append(record['map_result_id'])
            return record, [pointer, *results]

        return self._mutate(request, 'map-commit', 'source', request['snapshot_id'], update)

    def read_map(self, run_id, map_result_id):
        require(map_result_id in self.status(run_id)['map_result_ids'], 'unknown map result')
        value = _load_json(self.reports(run_id) / 'map-results' / (safe_id(map_result_id) + '.json'))
        require(value['map_result_id'] == 'map-' + fingerprint({k: v for k, v in value.items() if k != 'map_result_id'}),
                'mapping result fingerprint changed', 'SOURCE_CHANGED')
        self.validate_source_snapshot(run_id, value['snapshot_id'])
        for completion in value['native_completions']:
            self.native_evidence(self.vault, completion['proof'], {'task_body': completion['task_body'],
                'run_id': run_id, 'snapshot_id': value['snapshot_id']})
        for pointer in value['results']:
            read_pin(self.vault, pointer)
        return value

    def round_input(self, run_id, round_id):
        require(round_id in self.status(run_id)['round_ids'], 'unknown round')
        value = _load_json(self.root(run_id) / 'rounds' / safe_id(round_id) / 'input.json')
        require(value['input_fingerprint'] == fingerprint({k: v for k, v in value.items() if k != 'input_fingerprint'}),
                'round input changed', 'SOURCE_CHANGED')
        return value

    def freeze_round(self, request):
        def update(run, grant):
            require(run['active_round_id'] is None, 'this run already has an active round', 'ROUND_BUSY')
            # A Vault has one coordinator across runs, not one per run.
            for path in _vault_path(self.vault, ROOT).glob('*/run.json'):
                other = _load_json(path)
                require(not other.get('active_round_id'), 'Vault already has an active coordination round', 'ROUND_BUSY')
            maximum = request['max_maps']
            require(type(maximum) is int and maximum > 0 and maximum <= run['policy']['max_maps_per_round'],
                    'round map bound exceeds pinned policy')
            pending = [mid for mid in run['map_result_ids'] if mid not in run['consumed_map_ids']]
            selected = pending[:maximum]
            require(selected, 'no unconsumed source mapping is ready', 'NO_READY_WORK')
            maps = [self.read_map(run['run_id'], mid) for mid in selected]
            context_bound = run['policy'].get('max_context_maps', 8)
            require(type(context_bound) is int and context_bound >= 0, 'invalid fixed context map bound')
            context, context_holds = [], []
            candidates = run['consumed_map_ids'][-context_bound:] if context_bound else []
            for map_id in candidates:
                try:
                    context.append(self.read_map(run['run_id'], map_id))
                except ContractError as error:
                    context_holds.append({'map_result_id': map_id, 'code': error.code, 'reason': str(error)})
            round_id = safe_id(request['round_id'])
            value = {'contract': CONTRACTS['round'], 'run_id': run['run_id'], 'round_id': round_id,
                     'map_result_ids': selected, 'context_map_result_ids': [row['map_result_id'] for row in context],
                     'context_holds': context_holds,
                     'snapshots': sorted({row['snapshot_id'] for row in [*maps, *context]}),
                     'contracts': run['contracts'], 'budget_ids': sorted({row['budget_id'] for row in maps})}
            value['input_fingerprint'] = fingerprint(value)
            pointer = self._immutable(self.root(run['run_id']) / 'rounds' / round_id / 'input.json', value)
            run['round_ids'].append(round_id)
            run['active_round_id'] = round_id
            run['consumed_map_ids'].extend(selected)
            return value, [pointer]

        return self._mutate(request, 'freeze-round', 'round', request['round_id'], update)

    def settle_round(self, request):
        def update(run, grant):
            round_id = request['round_id']
            require(run['active_round_id'] == round_id, 'round is not the active coordinator')
            round_input = self.round_input(run['run_id'], round_id)
            disposition = request['disposition']
            require(disposition.get('round_input_fingerprint') == round_input['input_fingerprint']
                    and disposition.get('state') in ('delivered', 'held', 'reused')
                    and (disposition['state'] == 'delivered' or disposition.get('reason')), 'round disposition is incomplete')
            if disposition['state'] == 'delivered':
                delivered = read_pin(self.vault, disposition['delivery'])
                require(delivered.get('round_id') == round_id and delivered.get('state') == 'committed',
                        'round delivery has not committed')
            pointer = self._immutable(self.root(run['run_id']) / 'rounds' / round_id / 'settlement.json', disposition)
            run['active_round_id'] = None
            return disposition, [pointer]

        return self._mutate(request, 'settle-round', 'round', request['round_id'], update)

    def close_run(self, request):
        def update(run, grant):
            require(run['active_round_id'] is None
                    and all(row['state'] in ('mapped', 'failed', 'mapping_held') for row in run['sources'].values())
                    and set(run['map_result_ids']) == set(run['consumed_map_ids']),
                    'sources or rounds remain unfinished', 'INCOMPLETE_COVERAGE')
            accounting = request.get('usage_reconciliation')
            reserved = {key: row for key, row in run['reservations'].items() if row['state'] == 'reserved'}
            require(accounting or not reserved, 'reserved or unknown semantic consumption remains unsettled')
            dispositions = [self.root(run['run_id']) / 'rounds' / rid / 'settlement.json' for rid in run['round_ids']]
            require(all(path.is_file() for path in dispositions), 'round settlement is missing')
            gaps = [row for row in run['sources'].values() if row['state'] == 'failed']
            for row in run['sources'].values():
                if row['state'] == 'mapping_held':
                    hold = row['mapping_hold']
                    read_pin(self.vault, hold['wait'])
                    gaps.append(hold)
            maps = [self.read_map(run['run_id'], mid) for mid in run['map_result_ids']]
            gaps.extend(row for result in maps for row in result['coverage'] if row['disposition'] == 'held')
            gaps.extend(row for result in maps for row in result['unresolved'])
            settlements = [_load_json(path) for path in dispositions]
            gaps.extend(row for row in settlements if row['state'] == 'held')
            gaps.extend(change for row in settlements for change in row.get('held_changes', []))
            proof = request.get('native_drained')
            require(isinstance(proof, dict), 'native drain proof required')
            origin = _load_json(_vault_path(self.vault, f"_system/ledgers/ingest-workflows/{run['source_workflow_id']}.json"))
            boards = {origin['kanban']['board_id']}
            for result in maps:
                boards.update(row['board'] for row in result['native_completions'])
            for path in (self.root(run['run_id']) / 'native').rglob('operation.json'):
                operation = _load_json(path)
                require(operation.get('workflow_mode') == MODE
                        and operation.get('incremental', {}).get('run_id') == run['run_id'],
                        'native execution scope changed')
                boards.add(operation['board'])
            drain = verify_native_drain(self.vault, proof, run['source_workflow_id'], boards)
            final_delivery = request.get('final_delivery')
            delivered = [row for row in settlements if row['state'] == 'delivered']
            if delivered:
                require(final_delivery == delivered[-1]['delivery'], 'closure must use the last committed round delivery')
                delivery = read_pin(self.vault, final_delivery)
                from .vault_finalize import FileVaultFinalizeService

                finalize = FileVaultFinalizeService(self.vault)
                release_id = delivery.get('release_id')
                require(delivery.get('state') == 'committed' and release_id
                        and finalize.status()['current_release_id'] == release_id
                        and delivery.get('release_hash') == _sha(finalize._manifest_path(release_id).read_bytes())
                        and delivery.get('provider', {}).get('status') == 'ready'
                        and delivery['provider'].get('release_id') == release_id
                        and delivery['provider'].get('release_hash') == delivery['release_hash'],
                        'closure release and Provider generation differ', 'INDEX_MISMATCH')
                finalize.validate(release_id)
            else:
                require(final_delivery is None, 'closure has no delivered round')
            additional, retained = [], []
            if accounting:
                head = _load_json(self.root(run['run_id']) / 'control/head.json')
                require(head.get('partial_closure') == accounting and head['state'] == 'active',
                        'usage reconciliation requires the authorized closure checkpoint', 'ACCESS_DENIED')
                reconciliation = read_pin(self.vault, accounting)
                require(reconciliation['contract'] == 'incremental-closure-accounting/v1'
                        and reconciliation['run_id'] == run['run_id'] and reconciliation['actor'] == run['actor']
                        and reconciliation['run_revision'] == run['revision']
                        and set(reconciliation['entries']) == reserved.keys(),
                        'closure reconciliation differs from the original reservations')
                for key, entry in reconciliation['entries'].items():
                    row = reserved[key]
                    require(entry['reservation'] == row, 'closure reconciliation changed the original reservation')
                    for field in ('job', 'attempt', 'audit', 'failure', 'native_result'):
                        if entry.get(field):
                            read_pin(self.vault, entry[field])
                    for proof in entry.get('http_sends', []):
                        read_pin(self.vault, proof)
                    actual = entry.get('actual')
                    if actual is None:
                        retained.append(key)
                        continue
                    require(entry['disposition'] in ('settled_actual', 'settled_zero') and entry['audit'] and entry.get('session')
                            and (entry['disposition'] != 'settled_zero'
                                 or (not any(actual.values()) and entry['failure']
                                     and entry['http_journal']['send_count'] == 0 and not entry['http_sends']))
                            and set(actual) == {'calls', 'input_tokens', 'output_tokens'}
                            and all(type(value) is int and 0 <= value <= row['max_' + field]
                                    for field, value in actual.items()), 'closure usage exceeds its bound reservation')
                    usage = self._immutable(self.reports(run['run_id']) / 'closure-usage' / (key + '.json'),
                                            {'request_fingerprint': row['request_fingerprint'], 'actual': actual,
                                             'reconciliation': accounting, 'audit': entry['audit'],
                                             'session': entry['session']})
                    row.update(state='settled', actual=actual, usage=usage)
                    additional.append(usage)
                additional.append(accounting)
            result = {'contract': 'incremental-run-closure/v1', 'run_id': run['run_id'],
                      'state': 'partial' if gaps or retained else 'completed', 'source_outcomes': run['sources'],
                      'rounds': settlements, 'coverage_gaps': gaps, 'usage': run['reservations'],
                      'final_delivery': final_delivery, 'native_drained': drain}
            if accounting:
                result.update(usage_reconciliation=accounting, retained_reservation_ids=sorted(retained))
            if 'directory_navigation' in request:
                result['directory_navigation'] = request['directory_navigation']
            pointer = self._immutable(self.reports(run['run_id']) / 'closure.json', result)
            run.update(state=result['state'], closure=pointer)
            return result, [*additional, pointer]

        return self._mutate(request, 'close', 'run', request['run_id'], update)

    def preview_reuse(self, request):
        """Read-only inspection; it never changes source disposition or budgets."""
        results = []
        for snapshot_id in request.get('snapshots', []):
            try:
                value = self.validate_source_snapshot(request['run_id'], snapshot_id)
                results.append({'snapshot_id': snapshot_id, 'valid': True, 'fingerprint': fingerprint(value)})
            except ContractError as error:
                results.append({'snapshot_id': snapshot_id, 'valid': False, 'code': error.code, 'reason': str(error)})
        return {'run_id': request['run_id'], 'snapshots': results, 'read_only': True}
