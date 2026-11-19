"""Durable scoped admission for explicitly opted-in incremental runs."""
from contextlib import contextmanager
import os
from pathlib import Path
import re

from .file_locks import host_identity, process_exists
from .incremental_sources import read_pin, safe_id
from .source_units import _exclusive_lock, _json_bytes, _load_json, _sha, _vault_path, _write_atomic
from .validation import ContractError, fingerprint

STAGES = ('start', 'prepare', 'source-seal', 'mapping', 'map-commit', 'reserve', 'settle-budget',
          'freeze-round', 'coordination', 'writing', 'build', 'formal-build', 'release', 'index', 'acceptance',
          'settle-round', 'closure', 'close')
CHECKPOINTS = ('prepare', 'mapping', 'coordination', 'writing', 'formal-build', 'release', 'acceptance', 'closure')
CONTROL_LOCK_TIMEOUT = 30


class IncrementalControl:
    def __init__(self, vault, run_id):
        self.vault = vault
        self.run_id = safe_id(run_id)
        self.root = _vault_path(vault, f'_system/ledgers/incremental-ingest/{run_id}/control')
        self.path = self.root / 'head.json'

    def initialize(self, actor, pause_after, *, manual_sources=None):
        if (not isinstance(actor, str) or not actor.strip() or not isinstance(pause_after, list)
                or any(stage not in CHECKPOINTS for stage in pause_after) or len(set(pause_after)) != len(pause_after)):
            raise ContractError('INVALID_SCHEMA', '$.pause_after', 'unknown incremental checkpoint')
        admission = None
        if manual_sources is not None:
            if not isinstance(manual_sources, list) or not manual_sources:
                raise ContractError('INVALID_SCHEMA', '$.manual_sources', 'manual source admission needs a fixed nonempty manifest')
            admission = {}
            for source in manual_sources:
                if (not isinstance(source, dict) or set(source) != {'path', 'sha256'}
                        or not isinstance(source['path'], str) or not source['path']
                        or not isinstance(source['sha256'], str)
                        or not re.fullmatch(r'[0-9a-f]{64}', source['sha256'])
                        or source['path'] in admission):
                    raise ContractError('INVALID_SCHEMA', '$.manual_sources', 'manual sources need unique exact paths and hashes')
                _vault_path(self.vault, source['path'])
                admission[source['path']] = {'sha256': source['sha256'], 'state': 'held'}
        with _exclusive_lock(self.root / '.lock', timeout=CONTROL_LOCK_TIMEOUT):
            if self.path.exists():
                self._recover_pending()
                head = _load_json(self.path)
                if head['actor'] != actor or head['pause_after'] != pause_after:
                    raise ContractError('IDEMPOTENCY_CONFLICT', '$', 'incremental control policy changed')
                saved = head.get('manual_source_admission')
                saved_sources = None if saved is None else {path: row['sha256'] for path, row in saved.items()}
                requested_sources = None if admission is None else {path: row['sha256'] for path, row in admission.items()}
                if saved_sources != requested_sources:
                    raise ContractError('IDEMPOTENCY_CONFLICT', '$', 'manual source admission policy changed')
                return head
            head = {'contract': 'incremental-control/v1', 'run_id': self.run_id, 'actor': actor,
                    'revision': 1, 'state': 'active', 'pause_after': pause_after, 'boundary': None,
                    'released': [], 'active_scopes': {}}
            if admission is not None:
                head['manual_source_admission'] = admission
            _write_atomic(self.path, _json_bytes(head))
            return head

    def source_allowed(self, path, sha256):
        """A manual operator grant is independent of the current pause boundary."""
        admission = self.status().get('manual_source_admission')
        if admission is None:
            return True
        source = admission.get(path)
        return bool(source and source.get('sha256') == sha256 and source.get('state') == 'released')

    def status(self):
        head = _load_json(self.path)
        for path in sorted((self.root / 'operations').glob('*.json')):
            pending = _load_json(path)
            if pending.get('commit_state') != 'prepared':
                continue
            if head != pending['state'] and fingerprint(head) != pending['previous_head_fingerprint']:
                raise ContractError('REVISION_CONFLICT', '$', 'pending control intent differs from the saved head')
            # A prepared user stop already revokes admission even if the process
            # exited before replacing head.json. Read-only status never writes.
            head = pending['state']
        if head.get('contract') != 'incremental-control/v1' or head.get('run_id') != self.run_id:
            raise ContractError('STALE_INPUT', '$', 'incremental control identity changed')
        return head

    def _recover_pending(self):
        """Finish only explicitly requested control writes under the control lock."""
        for path in sorted((self.root / 'operations').glob('*.json')):
            pending = _load_json(path)
            if pending.get('commit_state') != 'prepared':
                continue
            current = _load_json(self.path)
            if current != pending['state'] and fingerprint(current) != pending['previous_head_fingerprint']:
                raise ContractError('REVISION_CONFLICT', '$', 'control head drifted across its pending write')
            _write_atomic(self.path, _json_bytes(pending['state']))
            _write_atomic(path, _json_bytes({**pending, 'commit_state': 'committed'}))

    def sources_active(self):
        run_path = self.root.parent / 'run.json'
        if not run_path.exists():
            return False
        run = _load_json(run_path)
        origin = _vault_path(self.vault, f"_system/ledgers/ingest-workflows/{run['source_workflow_id']}.json")
        if not origin.exists():
            return False
        board = _load_json(origin)['kanban']['board_id']
        if not board:
            return False
        from contextlib import closing
        from hermes_cli import kanban_db as kb, kanban_db_connect as kc
        from native_stop_inventory import workflow_processes

        with closing(kc.connect(board=board)) as connection:
            claimed = any(task.claim_lock or task.status == 'running' for task in kb.list_tasks(connection))
        return claimed or any(row['kind'] == 'native_worker'
                              for row in workflow_processes(run['source_workflow_id'], board))

    @staticmethod
    def _owner():
        boot = Path('/proc/sys/kernel/random/boot_id')
        stat = Path('/proc/self/stat')
        return {'pid': os.getpid(), 'host': host_identity(),
                'boot_id': boot.read_text().strip() if boot.is_file() else None,
                'process_start': stat.read_text().rsplit(')', 1)[1].split()[19] if stat.is_file() else None}

    @classmethod
    def _owner_alive(cls, owner):
        current = cls._owner()
        if not isinstance(owner, dict) or owner.get('host') != current['host']:
            raise ContractError('RUNTIME_UNVERIFIED', '$', 'executor host identity cannot be verified')
        if current['boot_id'] is not None and owner.get('boot_id') != current['boot_id']:
            return False
        if not process_exists(owner['pid']):
            return False
        if current['process_start'] is not None:
            try:
                fields = Path('/proc', str(owner['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
            except FileNotFoundError:
                return False
            return fields[0] != 'Z' and fields[19] == owner.get('process_start')
        return True

    def _scope_has_native_consumers(self, scope):
        from contextlib import closing
        from hermes_cli import kanban_db as kb, kanban_db_connect as kc
        from native_stop_inventory import workflow_processes

        for path in (self.root.parent / 'native').rglob('operation.json'):
            operation = _load_json(path)
            bound = operation.get('incremental', {})
            if bound.get('scope_kind') != scope['scope_kind'] or bound.get('scope_id') != scope['scope_id']:
                continue
            if bound.get('run_id') != self.run_id:
                raise ContractError('STALE_INPUT', '$', 'native scope changed during executor reconciliation')
            board = operation['board']
            with closing(kc.connect(board=board)) as connection:
                if any(task.status in ('ready', 'running', 'todo', 'triage') or task.claim_lock
                       for task in kb.list_tasks(connection)):
                    return True
            # This reconciliation concerns one original board. Other sources
            # and their source-workflow reconciler may still be progressing.
            if workflow_processes('', board):
                return True
        return False

    def reconcile_executors(self):
        """Retire dead controller scopes only after native work has drained.

        The caller holds the run's exclusive controller lock. This operation
        preserves pause/cancel, unknown model consumption and domain outcomes.
        """
        if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
            raise ContractError('ACCESS_DENIED', '$', 'executor reconciliation requires a trusted controller')
        with _exclusive_lock(self.root / '.lock', timeout=CONTROL_LOCK_TIMEOUT):
            self._recover_pending()
            head = self.status()
            removed = {}
            for key, scope in head['active_scopes'].items():
                if self._owner_alive(scope.get('owner')):
                    continue
                if self._scope_has_native_consumers(scope):
                    raise ContractError('NATIVE_ACTIVE', '$', 'orphaned scope still has native work; reconcile its original tasks')
                removed[key] = scope
            if removed:
                record = {'contract': 'incremental-executor-reconciliation/v1', 'run_id': self.run_id,
                          'control_revision': head['revision'], 'retired_executors': removed}
                _write_atomic(self.root / 'executor-reconciliations' / (fingerprint(record) + '.json'), _json_bytes(record))
                for key in removed:
                    head['active_scopes'].pop(key)
                head['revision'] += 1
                _write_atomic(self.path, _json_bytes(head))
            return head

    def check_admission(self, scope, stage=None, input_fingerprint=None, **kwargs):
        if isinstance(scope, str):
            scope = {'scope_kind': kwargs.get('scope_kind', 'run'), 'scope_id': scope}
        view = {**scope, 'stage': stage or scope.get('stage'),
                'input_fingerprint': input_fingerprint or scope.get('input_fingerprint')}
        head = self.status()
        if (view.get('run_id', self.run_id) != self.run_id
                or view.get('actor', head['actor']) != head['actor']):
            raise ContractError('ACCESS_DENIED', '$', 'control scope belongs to another run or actor')
        if (view.get('scope_kind') not in ('run', 'source', 'round', 'page')
                or not isinstance(view.get('scope_id'), str) or not view['scope_id']):
            raise ContractError('INVALID_SCHEMA', '$', 'exact incremental scope identity required')
        active = any(row['scope_kind'] == view['scope_kind'] and row['scope_id'] == view['scope_id']
                     for row in head['active_scopes'].values())
        if head['state'] != 'active' and not (head['state'] == 'pausing' and active):
            raise ContractError('INCREMENTAL_PAUSED', '$', 'incremental control holds this scope')
        if view['stage'] not in STAGES or not view['input_fingerprint']:
            raise ContractError('INVALID_SCHEMA', '$', 'exact scope and input fingerprint required')
        return {**view, 'contract': 'incremental-control-permission/v1', 'run_id': self.run_id, 'allowed': True,
                'control_revision': head['revision']}

    @contextmanager
    def executing(self, scope, stage, input_fingerprint):
        key = fingerprint({'scope': scope, 'stage': stage, 'input_fingerprint': input_fingerprint})
        with _exclusive_lock(self.root / '.lock', timeout=CONTROL_LOCK_TIMEOUT):
            self._recover_pending()
            permission = self.check_admission(scope, stage, input_fingerprint)
            head = self.status()
            if head['state'] != 'active':
                raise ContractError('INCREMENTAL_PAUSED', '$', 'new executor admission is held while pausing')
            if key in head['active_scopes']:
                raise ContractError('WORKER_ACTIVE', '$', 'scope already has an executor')
            permission = {**permission, 'owner': self._owner()}
            head['active_scopes'][key] = permission
            _write_atomic(self.path, _json_bytes(head))
        try:
            yield permission
        finally:
            with _exclusive_lock(self.root / '.lock', timeout=CONTROL_LOCK_TIMEOUT):
                self._recover_pending()
                head = self.status()
                head['active_scopes'].pop(key, None)
                _write_atomic(self.path, _json_bytes(head))

    def checkpoint(self, scope, stage, evidence):
        if stage not in CHECKPOINTS:
            raise ContractError('INVALID_SCHEMA', '$.stage', 'unsupported incremental checkpoint')
        boundary = {'scope': scope, 'stage': stage, 'input_fingerprint': fingerprint(evidence), 'evidence': evidence}
        with _exclusive_lock(self.root / '.lock', timeout=CONTROL_LOCK_TIMEOUT):
            self._recover_pending()
            head = self.status()
            if head['state'] == 'pausing':
                if not head['active_scopes'] and not self.sources_active():
                    head.update(state='paused', revision=head['revision'] + 1)
                    _write_atomic(self.path, _json_bytes(head))
                return head
            if head['state'] != 'active':
                return head
            if stage not in head['pause_after'] or fingerprint(boundary) in head['released']:
                return head
            head.update(state='pausing' if head['active_scopes'] or self.sources_active() else 'paused', boundary=boundary,
                        revision=head['revision'] + 1)
            _write_atomic(self.path, _json_bytes(head))
            return head

    def control(self, request):
        safe_id(request['operation_id'])
        if request.get('run_id', self.run_id) != self.run_id:
            raise ContractError('STALE_INPUT', '$', 'control request belongs to another run')
        with _exclusive_lock(self.root / '.lock', timeout=CONTROL_LOCK_TIMEOUT):
            self._recover_pending()
            head = self.status()
            before = fingerprint(head)
            identity = fingerprint(request)
            receipt = self.root / 'operations' / (request['operation_id'] + '.json')
            if receipt.exists():
                result = _load_json(receipt)
                if result['request_sha256'] != identity:
                    raise ContractError('IDEMPOTENCY_CONFLICT', '$', 'control operation changed')
                return result['state']
            if request['actor'] != head['actor'] or request['expected_revision'] != head['revision']:
                raise ContractError('REVISION_CONFLICT', '$', 'control actor or revision changed')
            action = request['action']
            if action == 'continue':
                if head['state'] != 'paused' or request.get('boundary_fingerprint') != fingerprint(head['boundary']):
                    raise ContractError('STALE_INPUT', '$', 'continue requires the exact acknowledged boundary')
                if 'writing_amendment' in request:
                    if {'writing_continuation', 'repair_writing'} & request.keys():
                        raise ContractError('INVALID_TRANSITION', '$', 'continue requires one writing recovery intent')
                    self._authorize_writing_amendment(head, request)
                elif 'writing_continuation' in request:
                    if 'repair_writing' in request:
                        raise ContractError('INVALID_TRANSITION', '$', 'continue requires one writing recovery intent')
                    self._authorize_writing_continuation(head, request)
                elif 'repair_writing' in request:
                    self._authorize_writing_repair(head, request)
                else:
                    self._require_writing_recovery(head)
                head['released'].append(fingerprint(head['boundary']))
                head.update(state='active', boundary=None)
            elif action == 'pause':
                head.update(state='pausing' if head['active_scopes'] or self.sources_active() else 'paused',
                            boundary={'scope': {'scope_kind': 'run', 'scope_id': self.run_id},
                            'stage': 'manual', 'input_fingerprint': identity, 'evidence': {}})
            elif action == 'cancel':
                head.update(state='cancelled')
            elif action == 'release-source':
                if head['state'] == 'cancelled':
                    raise ContractError('WORKFLOW_STOPPED', '$', 'cancelled runs cannot release sources')
                admission = head.get('manual_source_admission')
                path, sha256 = request.get('source_path'), request.get('source_sha256')
                if (not isinstance(admission, dict) or not isinstance(path, str)
                        or not isinstance(sha256, str) or path not in admission
                        or admission[path]['sha256'] != sha256):
                    raise ContractError('ACCESS_DENIED', '$', 'source release must match the fixed manifest and exact hash')
                run = _load_json(self.root.parent / 'run.json')
                if (run.get('run_id') != self.run_id or run.get('actor') != head['actor']
                        or {'path': path, 'sha256': sha256} not in run.get('source_manifest', [])):
                    raise ContractError('ACCESS_DENIED', '$', 'source release must match the fixed manifest and exact hash')
                if admission[path]['state'] != 'held':
                    raise ContractError('INVALID_TRANSITION', '$', 'source already released; replay the original operation')
                admission[path]['state'] = 'released'
            else:
                raise ContractError('INVALID_SCHEMA', '$.action', 'unknown control action')
            head['revision'] += 1
            transaction = {'request_sha256': identity, 'state': head, 'previous_head_fingerprint': before,
                           'commit_state': 'prepared'}
            _write_atomic(receipt, _json_bytes(transaction))
            _write_atomic(self.path, _json_bytes(head))
            _write_atomic(receipt, _json_bytes({**transaction, 'commit_state': 'committed'}))
            return head

    def _require_writing_recovery(self, head):
        if head['boundary']['stage'] != 'manual' or not head.get('writing_repairs'):
            return
        round_id = _load_json(self.root.parent / 'run.json').get('active_round_id')
        repair = head['writing_repairs'].get(round_id)
        if repair is None:
            return
        round_id = safe_id(round_id)
        continuation = head.get('writing_continuations', {}).get(round_id)
        operation_id = safe_id((continuation or repair)['operation_id'])
        root = self.root.parent
        if ((root / 'native/round' / round_id / 'writing/recoveries' / operation_id / 'stop.json').is_file()
                and not (root / 'rounds' / round_id / 'writing-revision.json').exists()):
            raise ContractError('RECOVERY_REQUIRED', '$', 'stopped writing revision requires recover-writing')

    def _authorize_writing_continuation(self, head, request):
        pointer = request['writing_continuation']
        manifest = read_pin(self.vault, pointer)
        try:
            round_id = safe_id(manifest['round_id'])
            repair = head.get('writing_repairs', {}).get(round_id)
            round_root = self.root.parent / 'rounds' / round_id
            run = _load_json(self.root.parent / 'run.json')
            expected_path = round_root / 'writing-continuations' / (request['operation_id'] + '.json')
            if (manifest['contract'] != 'incremental-writing-continuation/v1'
                    or pointer['ref'] != expected_path.relative_to(self.vault).as_posix()
                    or manifest['run_id'] != self.run_id or manifest['actor'] != head['actor']
                    or manifest['operation_id'] != request['operation_id']
                    or manifest['control_revision'] != head['revision'] or manifest['run_revision'] != run['revision']
                    or manifest['boundary_fingerprint'] != request['boundary_fingerprint']
                    or run.get('active_round_id') != round_id or run['state'] != 'active'
                    or head['boundary']['stage'] != 'manual' or head['active_scopes'] or 'writing' not in head['pause_after']
                    or repair is None or repair != manifest['original_authorization']
                    or (round_root / 'writing-revision.json').exists() or (round_root / 'formal-request.json').exists()
                    or round_id in head.get('writing_continuations', {})):
                raise ContractError('INVALID_TRANSITION', '$', 'writing continuation differs from the drained authorized revision')
            writer = self.root.parent / 'native/round' / round_id / 'writing/recoveries' / safe_id(repair['operation_id'])
            if (manifest['stop']['ref'] != (writer / 'stop.json').relative_to(self.vault).as_posix()
                    or manifest['previous_operation']['ref'] != (writer / 'operation.json').relative_to(self.vault).as_posix()):
                raise ContractError('STALE_INPUT', '$', 'writing continuation changes the original workspace')
            read_pin(self.vault, manifest['stop'])
            read_pin(self.vault, manifest['previous_operation'])
        except KeyError as error:
            raise ContractError('INVALID_SCHEMA', '$', 'missing writing continuation binding') from error
        head.setdefault('writing_continuations', {})[round_id] = {
            'operation_id': request['operation_id'], 'manifest': pointer}

    def _authorize_writing_amendment(self, head, request):
        pointer = request['writing_amendment']
        manifest = read_pin(self.vault, pointer)
        round_id = safe_id(manifest['round_id'])
        root = self.root.parent / 'rounds' / round_id
        run = _load_json(self.root.parent / 'run.json')
        prior = head.get('writing_amendments', {}).get(round_id)
        boundary = head['boundary']
        expected = root / 'writing-amendments' / request['operation_id'] / 'manifest.json'
        disposition = (root / 'writing-amendments' / safe_id(prior['operation_id']) / 'disposition.json'
                       if prior else root / ('writing-revision.json' if round_id in head.get('writing_repairs', {})
                                             else 'writing-disposition.json'))
        if (manifest['contract'] != 'incremental-writing-amendment/v1'
                or pointer['ref'] != expected.relative_to(self.vault).as_posix()
                or manifest['run_id'] != self.run_id or manifest['actor'] != head['actor']
                or manifest['operation_id'] != request['operation_id']
                or manifest['control_revision'] != head['revision'] or manifest['run_revision'] != run['revision']
                or manifest['boundary_fingerprint'] != request['boundary_fingerprint']
                or run['active_round_id'] != round_id or run['state'] != 'active'
                or boundary['stage'] != 'writing' or boundary['scope'] != {'scope_kind': 'round', 'scope_id': round_id}
                or head['active_scopes'] or 'writing' not in head['pause_after']
                or (root / 'formal-request.json').exists() or (root / 'delivery.json').exists()
                or manifest['previous_amendment'] != (prior['manifest'] if prior else None)
                or manifest['prior_disposition']['ref'] != disposition.relative_to(self.vault).as_posix()
                or manifest['prior_disposition']['sha256'] != boundary['evidence'].get('writing_disposition_sha256')):
            raise ContractError('INVALID_TRANSITION', '$', 'writing amendment requires the exact drained writing checkpoint')
        read_pin(self.vault, manifest['prior_disposition'])
        head.setdefault('writing_amendments', {})[round_id] = {
            'operation_id': request['operation_id'], 'manifest': pointer}

    def _authorize_writing_repair(self, head, request):
        boundary = head['boundary']
        scope = boundary['scope']
        pages = request['repair_writing']
        if (boundary['stage'] != 'writing' or scope['scope_kind'] != 'round'
                or head['active_scopes'] or 'writing' not in head['pause_after']
                or not isinstance(pages, list) or not pages
                or any(not isinstance(page, str) or not re.fullmatch(r'[0-9a-f]{64}', page) for page in pages)
                or len(pages) != len(set(pages))):
            raise ContractError('INVALID_TRANSITION', '$', 'writing repair requires exact held plans at a drained writing pause')
        round_id = safe_id(scope['scope_id'])
        run = _load_json(self.root.parent / 'run.json')
        root = self.root.parent / 'rounds' / round_id
        original = root / 'writing-disposition.json'
        digest = boundary['evidence'].get('writing_disposition_sha256')
        if (run['active_round_id'] != round_id or (root / 'formal-request.json').exists()
                or (root / 'writing-revision.json').exists()
                or round_id in head.get('writing_repairs', {}) or not original.is_file()
                or _sha(original.read_bytes()) != digest):
            raise ContractError('STALE_INPUT', '$', 'writing repair requires the original paused disposition before Build')
        held = {row['change_id'] for row in _load_json(original)['held']}
        plans = {row['change_id'] for row in _load_json(root / 'page-changes.json')
                 if row['action'] in ('create', 'update')}
        if not set(pages) <= held & plans:
            raise ContractError('ACCESS_DENIED', '$', 'writing repair can only select held create or update plans')
        head.setdefault('writing_repairs', {})[round_id] = {
            'operation_id': request['operation_id'], 'plan_ids': pages, 'prior_disposition_sha256': digest}
