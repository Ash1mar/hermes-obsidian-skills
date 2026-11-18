"""Serialized incremental Build, publication and index delivery with durable recovery."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import re
from typing import Callable, Mapping

from .source_units import _exclusive_lock, _json_bytes, _load_json, _sha, _vault_path, _write_atomic
from .validation import ContractError, fingerprint


WINDOW_REF = '_system/metadata/incremental-delivery-state.json'
ROOT = '_system/ledgers/incremental-ingest'
TERMINAL = {'committed', 'aborted'}
STEPS = ('prepared', 'build_committing', 'build_committed', 'release_applying',
         'release_applied', 'index_pending', 'index_verified', 'committed')
_WRITER = ContextVar('incremental_delivery_writer', default=None)


def fail(code, message):
    raise ContractError(code, '$', message)


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value):
        fail('INVALID_SCHEMA', 'invalid incremental delivery identity')
    return value


def _immutable(path, value):
    if path.is_file():
        if _load_json(path) != value:
            fail('IDEMPOTENCY_CONFLICT', 'delivery operation already has different immutable contents')
    else:
        _write_atomic(path, _json_bytes(value))


def window(vault):
    """Read a validated durable window; malformed or stale metadata fails closed."""
    root = Path(vault).resolve()
    path = _vault_path(root, WINDOW_REF)
    if not path.is_file():
        return None
    # Delivery admission is current control state, never a captured evidence read.
    head = json.loads(path.read_text(encoding='utf-8'))
    if head.get('contract') != 'incremental-delivery-state/v1':
        fail('INVALID_STATE', 'unknown incremental delivery window contract')
    record = json.loads(_vault_path(root, head['delivery_ref']).read_text(encoding='utf-8'))
    if (record.get('contract') != 'incremental-round-delivery/v1'
            or record.get('delivery_id') != head.get('delivery_id')
            or record.get('revision', 0) < head.get('revision', 0)):
        fail('INVALID_STATE', 'incremental delivery window differs from its durable round')
    # A crash can leave a conservative old head. Never expose reads until both agree.
    if record.get('state') != head.get('state'):
        return {**head, 'state': 'blocked', 'query_ready': False, 'reason': 'delivery-head-reconciliation-required'}
    return head


def readiness(vault):
    value = window(vault)
    if value is None or value['state'] in TERMINAL:
        return {'available': True, 'status': 'ready', 'reason': None}
    return {'available': False, 'status': 'index_pending' if value['state'] == 'index_pending'
            else 'delivery_in_progress', 'reason': value.get('reason') or 'incremental-delivery-in-progress',
            'delivery_id': value['delivery_id'], 'step': value['state']}


def require_query_available(vault):
    state = readiness(vault)
    if not state['available']:
        fail('DELIVERY_IN_PROGRESS', state['reason'])


def require_index_admission(vault, release_id, release_hash):
    value = window(vault)
    if value is None or value['state'] in TERMINAL:
        return
    if (value['state'] != 'index_pending' or not release_id or not release_hash
            or value.get('release_id') != release_id or value.get('release_hash') != release_hash):
        fail('DELIVERY_IN_PROGRESS', 'only the exact pending incremental release may be indexed')


def require_build_writer(vault, run_id):
    value = window(vault)
    if value is None or value['state'] in TERMINAL:
        return
    require_delivery_owner(vault, run_id)
    if value['state'] != 'build_committing':
        fail('DELIVERY_IN_PROGRESS', 'Build commit is outside its fixed delivery step')


def require_delivery_owner(vault, run_id):
    value = window(vault)
    active = _WRITER.get()
    if (value is None or value['state'] in TERMINAL or active is None
            or active['vault'] != str(Path(vault).resolve())
            or active['delivery_id'] != value['delivery_id'] or active['build_run_id'] != run_id
            or value['state'] not in ('build_committing', 'build_committed', 'release_applying')):
        fail('DELIVERY_IN_PROGRESS', 'mutation is outside the active incremental delivery owner')


def recover_parent_prefix(vault, run_id, page, review):
    """Permit an exact canonical-before-registry prefix using its immutable parent."""
    require_build_writer(vault, run_id)
    active = _WRITER.get()
    if active is None:
        return False
    target = next((row for row in active['targets'] if row['page_id'] == page['page_id']), None)
    if not target or any(target.get(key) != page.get(key) for key in
                         ('revision_id', 'parent_revision_id', 'authored_sha256', 'path')):
        return False
    if target['parent_authored_sha256'] != review.get('parent_authored_sha256'):
        return False
    from .knowledge_build import BUILD_ROOT
    parent = _load_json(_vault_path(vault, f"{BUILD_ROOT}/page-revisions/{page['page_id']}/{page['parent_revision_id']}.json"))
    canonical = _vault_path(vault, page['path'])
    return (parent['state'] == 'committed' and parent['authored_sha256'] == target['parent_authored_sha256']
            and canonical.is_file() and _sha(canonical.read_bytes()) == target['authored_sha256'])


class IncrementalDeliveryService:
    """All mutations require separate live native and scoped-control admission."""

    def __init__(self, vault, *, admission: Callable, native_check: Callable):
        from .vault_finalize import FileVaultFinalizeService

        if not callable(admission) or not callable(native_check):
            raise TypeError('incremental delivery needs control and native admission')
        self.finalize = FileVaultFinalizeService(vault)
        self.build = self.finalize.build
        self.vault = self.finalize.vault
        self.admission = admission
        self.native_check = native_check
        self.lock = _vault_path(self.vault, '_system/ledgers/incremental-ingest/.delivery.lock')

    def _path(self, run_id, round_id):
        return _vault_path(self.vault, f'{ROOT}/{safe_id(run_id)}/rounds/{safe_id(round_id)}/delivery.json')

    def status(self, run_id, round_id):
        return _load_json(self._path(run_id, round_id))

    def _check(self, value, binding, stage):
        if not isinstance(binding, Mapping) or not binding.get('task_id'):
            fail('ACCESS_DENIED', 'delivery owner requires an explicit native task binding')
        if self.native_check(binding) is False:
            fail('ACCESS_DENIED', 'native incremental delivery binding was rejected')
        scope = {'run_id': value['run_id'], 'scope_kind': 'round', 'scope_id': value['round_id'],
                 'stage': stage, 'input_fingerprint': value['input_fingerprint']}
        allowed = self.admission(scope)
        if allowed is False or isinstance(allowed, Mapping) and not allowed.get('allowed', True):
            fail('WORKFLOW_PAUSED', 'incremental round is not admitted by the control service')

    def _persist(self, value, state, **changes):
        path = self._path(value['run_id'], value['round_id'])
        updated = {**value, **changes, 'state': state, 'revision': value['revision'] + 1}
        receipt = path.parent / 'delivery-steps' / f"{updated['revision']:04d}.json"
        _immutable(receipt, updated)
        _write_atomic(path, _json_bytes(updated))
        if state != 'prepared':
            head = {'contract': 'incremental-delivery-state/v1', 'delivery_id': updated['delivery_id'],
                    'delivery_ref': path.relative_to(self.vault).as_posix(), 'revision': updated['revision'],
                    'run_id': value['run_id'], 'round_id': value['round_id'], 'state': state,
                    'owner': updated['owner'],
                    'query_ready': state in TERMINAL,
                    'release_id': updated.get('release_id'), 'release_hash': updated.get('release_hash')}
            _write_atomic(_vault_path(self.vault, WINDOW_REF), _json_bytes(head))
        return updated

    def _recover_head(self, value):
        directory = self._path(value['run_id'], value['round_id']).parent / 'delivery-steps'
        for path in sorted(directory.glob('*.json')):
            receipt = _load_json(path)
            if receipt['revision'] <= value['revision']:
                continue
            if (receipt['revision'] != value['revision'] + 1
                    or receipt['input_fingerprint'] != value['input_fingerprint']):
                fail('INVALID_STATE', 'delivery transition receipts are discontinuous')
            value = receipt
        _write_atomic(self._path(value['run_id'], value['round_id']), _json_bytes(value))
        head_path = _vault_path(self.vault, WINDOW_REF)
        head = _load_json(head_path) if head_path.is_file() else None
        if value['state'] != 'prepared' and (head is None or head.get('delivery_id') == value['delivery_id']):
            if head is not None and head.get('revision', 0) > value['revision']:
                fail('INVALID_STATE', 'delivery head is newer than every retained receipt')
            _write_atomic(head_path, _json_bytes({
                'contract': 'incremental-delivery-state/v1', 'delivery_id': value['delivery_id'],
                'delivery_ref': self._path(value['run_id'], value['round_id']).relative_to(self.vault).as_posix(),
                'run_id': value['run_id'], 'round_id': value['round_id'], 'owner': value['owner'],
                'revision': value['revision'],
                'state': value['state'], 'query_ready': value['state'] in TERMINAL,
                'release_id': value.get('release_id'), 'release_hash': value.get('release_hash')}))
        return value

    def _base(self, expected, expected_hash):
        actual = self.finalize.status()['current_release_id']
        if actual != expected:
            fail('REVISION_CONFLICT', 'incremental delivery base release changed')
        if actual and _sha(self.finalize._manifest_path(actual).read_bytes()) != expected_hash:
            fail('SOURCE_CHANGED', 'incremental delivery base release hash changed')
        if not actual and expected_hash is not None:
            fail('INVALID_SCHEMA', 'an empty base release cannot have a hash')

    def _targets(self, run, reviews):
        by_page = {row['page_id']: row for row in reviews}
        if len(by_page) != len(reviews) or set(by_page) != {p['page_id'] for p in run['page_revisions']}:
            fail('INVALID_SCOPE', 'delivery reviews must bind the exact Build pages')
        from .finalize_revision_state import content_path

        targets = []
        for page in run['page_revisions']:
            review = by_page[page['page_id']]
            draft = content_path(self.build, run, page)
            if _sha(draft.read_bytes()) != page['authored_sha256'] or review['authored_sha256'] != page['authored_sha256']:
                fail('SOURCE_CHANGED', 'incremental Build content differs from its reviewed bytes')
            targets.append({**{key: page[key] for key in ('page_id', 'subject_id', 'revision_id',
                                                          'parent_revision_id', 'path', 'authored_sha256')},
                            'parent_authored_sha256': review.get('parent_authored_sha256'),
                            'content_ref': draft.relative_to(self.vault).as_posix()})
        return targets

    def prepare(self, request, binding):
        """Pin a drafted native Build and all reviews before changing canonical pages."""
        required = {'operation_id', 'actor', 'run_id', 'round_id', 'authorization', 'build_run_id',
                    'finalize_request', 'base_release', 'base_release_sha256'}
        if (not required <= set(request) or not set(request) - required <= {'source_changes', 'authorized_removed_pages'}
                or request['authorization'].get('contract') != 'incremental-build-authorization/v1'):
            fail('INVALID_SCHEMA', 'incremental delivery requires its exact Build authorization')
        if request.get('source_changes'):
            if (request['authorization'].get('source_changes') != request['source_changes']
                    or request['authorization'].get('authorized_removed_pages') != request.get('authorized_removed_pages')):
                fail('INVALID_SCOPE', 'source replacement or withdrawal requires exact affected-page authorization')
        elif request.get('authorized_removed_pages'):
            fail('INVALID_SCOPE', 'additive delivery cannot authorize arbitrary page removal')
        for key in ('operation_id', 'run_id', 'round_id'):
            safe_id(request[key])
        path = self._path(request['run_id'], request['round_id'])
        if path.is_file():
            with _exclusive_lock(self.lock):
                existing = _load_json(path)
                if any(existing.get(key) != value for key, value in request.items()):
                    fail('IDEMPOTENCY_CONFLICT', 'round already pins a different delivery')
                self._check(existing, binding, 'formal-build')
                return self._recover_head(existing)
        run = _load_json(self.build._run_path(request['build_run_id']))
        final = request['finalize_request']
        if (run['state'] != 'draft' or final['run_id'] != run['run_id'] or final['actor'] != request['actor']
                or run['actor'] != request['actor'] or final['expected_revision'] != run['revision']):
            fail('INVALID_STATE', 'delivery must pin the current uncommitted Build and reviews')
        from .short_build import evidence
        if evidence(self.build, run)['scope']['authorization'] != request['authorization']:
            fail('INVALID_SCOPE', 'delivery authorization differs from the native incremental Build')
        targets = self._targets(run, final['reviews'])
        fingerprint_value = fingerprint({'request': request, 'targets': targets})
        value = {'contract': 'incremental-round-delivery/v1', **request, 'targets': targets,
                 'input_fingerprint': fingerprint_value, 'delivery_id': 'delivery-' + fingerprint_value,
                 'owner': {'task_id': binding.get('task_id'), 'binding_sha256': fingerprint(binding)},
                 'registry_revision': self.build._registry()['revision'], 'revision': 0, 'state': 'prepared'}
        self._check(value, binding, 'formal-build')
        with _exclusive_lock(self.lock):
            path = self._path(value['run_id'], value['round_id'])
            if path.is_file():
                existing = _load_json(path)
                if existing['input_fingerprint'] != fingerprint_value:
                    fail('IDEMPOTENCY_CONFLICT', 'round already pins a different delivery')
                return self._recover_head(existing)
            self._base(value['base_release'], value['base_release_sha256'])
            active = window(self.vault)
            if active and active['state'] not in TERMINAL:
                fail('DELIVERY_BUSY', 'another incremental delivery owns this Vault')
            return self._persist(value, 'prepared')

    def _target_prefix(self, value, *, completed=False):
        registry = self.build._registry()
        expected = value['registry_revision']
        if (value['state'] == 'prepared' and registry['revision'] != expected
                or value['state'] != 'prepared' and registry['revision'] not in (expected, expected + 1)):
            fail('REVISION_CONFLICT', 'identity registry changed outside the pinned Build commit prefix')
        subjects = {row['page_id']: row for row in registry['subjects']}
        for target in value['targets']:
            body = _vault_path(self.vault, target['content_ref'])
            if _sha(body.read_bytes()) != target['authored_sha256']:
                fail('SOURCE_CHANGED', 'retained Build content changed during delivery')
            canonical = _vault_path(self.vault, target['path'])
            allowed = {target['authored_sha256']}
            if target['parent_authored_sha256']:
                allowed.add(target['parent_authored_sha256'])
            if canonical.exists() and _sha(canonical.read_bytes()) not in allowed:
                fail('SOURCE_CHANGED', 'canonical page differs from either bound delivery version')
            subject = subjects.get(target['page_id'])
            allowed_revisions = {target['parent_revision_id'], target['revision_id']}
            if subject and subject['current_revision_id'] not in allowed_revisions:
                fail('REVISION_CONFLICT', 'page parent changed during incremental delivery')
            if completed and (not subject or subject['current_revision_id'] != target['revision_id']
                              or not canonical.is_file() or _sha(canonical.read_bytes()) != target['authored_sha256']):
                fail('INVALID_STATE', 'Build did not commit the exact bound page revision')

    @contextmanager
    def _writing(self, value):
        token = _WRITER.set({'vault': str(self.vault), 'delivery_id': value['delivery_id'],
                             'build_run_id': value['build_run_id'], 'targets': value['targets']})
        try:
            yield
        finally:
            _WRITER.reset(token)

    def deliver(self, run_id, round_id, binding, *, synchronize, build=None, stop_after=None):
        """Continue the fixed delivery prefix without repeating semantic work."""
        if stop_after not in (None, 'build_committed', 'index_verified'):
            fail('INVALID_SCOPE', 'unknown incremental delivery checkpoint')
        with _exclusive_lock(self.lock):
            value = self.status(run_id, round_id)
            self._check(value, binding, 'formal-build' if value['state'] in ('prepared', 'build_committing') else 'release')
            value = self._recover_head(value)
            if value['state'] in TERMINAL:
                return value
            active = window(self.vault)
            if active and active['state'] not in TERMINAL and active['delivery_id'] != value['delivery_id']:
                fail('DELIVERY_BUSY', 'another delivery owns the Vault')
            owner = {'task_id': binding['task_id'], 'binding_sha256': fingerprint(binding)}
            if value['owner'] != owner:
                value = self._persist(value, value['state'], owner=owner)
            self._target_prefix(value)
            if value['state'] == 'prepared':
                self._base(value['base_release'], value['base_release_sha256'])
                value = self._persist(value, 'build_committing')
            if value['state'] == 'build_committing':
                self._check(value, binding, 'formal-build')
                with self._writing(value):
                    result = build(value) if build else self.build.finalize(value['finalize_request'])
                self._check(value, binding, 'formal-build')
                if result.get('run', {}).get('run_id', value['build_run_id']) != value['build_run_id']:
                    fail('INVALID_STATE', 'native Build returned a different run identity')
                self.build.validate_run(value['build_run_id'])
                self._target_prefix(value, completed=True)
                value = self._persist(value, 'build_committed', build_result=result)
            if value['state'] == 'build_committed' and stop_after == 'build_committed':
                return value
            if value['state'] == 'build_committed':
                self._check(value, binding, 'release')
                self._base(value['base_release'], value['base_release_sha256'])
                release_id = 'release-' + value['delivery_id']
                request = {'release_id': release_id, 'actor': value['actor'],
                           'expected_state_revision': self.finalize.status()['revision'],
                           'build_run_ids': [value['build_run_id']],
                           'source_changes': value.get('source_changes', []), 'reason': 'Exact incremental round delivery'}
                value = self._persist(value, 'release_applying', release_request=request, release_id=release_id)
            if value['state'] == 'release_applying':
                self._check(value, binding, 'release')
                self._publish(value)
                release_hash = _sha(self.finalize._manifest_path(value['release_id']).read_bytes())
                value = self._persist(value, 'release_applied', release_hash=release_hash)
            if value['state'] == 'release_applied':
                value = self._persist(value, 'index_pending')
            if value['state'] == 'index_pending':
                self._check(value, binding, 'release')
                self._release_exact(value)
                provider = synchronize(value['release_id'], value['release_hash'])
                self._check(value, binding, 'release')
                if (provider.get('status') != 'ready' or provider.get('release_id') != value['release_id']
                        or provider.get('release_hash') != value['release_hash'] or not provider.get('index_generation')):
                    fail('INDEX_MISMATCH', 'Provider did not verify the exact incremental release')
                value = self._persist(value, 'index_verified', provider=provider)
            if value['state'] == 'index_verified' and stop_after == 'index_verified':
                return value
            if value['state'] == 'index_verified':
                self._check(value, binding, 'acceptance')
                self._release_exact(value)
                value = self._persist(value, 'committed')
            return value

    def _release_exact(self, value):
        if (self.finalize.status()['current_release_id'] != value['release_id']
                or _sha(self.finalize._manifest_path(value['release_id']).read_bytes()) != value['release_hash']):
            fail('REVISION_CONFLICT', 'release changed during incremental delivery')
        self.finalize.validate(value['release_id'])

    def _publish(self, value):
        release_id = value['release_id']
        current = self.finalize.status()['current_release_id']
        if current == release_id:
            self.finalize.validate(release_id)
            return
        self._base(value['base_release'], value['base_release_sha256'])
        plan_path = self.finalize._plan_path(release_id)
        plan = _load_json(plan_path) if plan_path.is_file() else self.finalize.plan(value['release_request'])['plan']
        if plan.get('blockers'):
            fail('PUBLICATION_BLOCKED', 'incremental release has unresolved required dependencies')
        if value['base_release']:
            base = _load_json(self.finalize._manifest_path(value['base_release']))
            before = {row['page_id'] for row in base['subjects']}
            eligible = {row['id'] for row in plan['index_eligibility'] if row['kind'] == 'knowledge_page' and row['eligible']}
            allowed_removals = set(value.get('authorized_removed_pages', []))
            if value.get('isolation'):
                from .short_build import content_isolation
                run = _load_json(self.build._run_path(value['build_run_id']))
                pointer, rows = content_isolation(self.build, run)
                isolated = {row['page_id'] for row in rows}
                if pointer != value['isolation'] or not isolated <= set(value['authorization'].get('isolation_pages', [])):
                    fail('ACCESS_DENIED', 'release isolation differs from the exact preauthorized committed scope')
                self._target_prefix(value, completed=True)
                allowed_removals.update(isolated)
            if not before - eligible <= allowed_removals:
                fail('INVALID_SCOPE', 'additive incremental delivery would remove a qualified page')
        self.finalize.apply({'release_id': release_id, 'actor': value['actor'], 'plan_id': plan['plan_id'],
                             'expected_revision': plan['revision']})
        self.finalize.validate(release_id)

    def recover(self, run_id, round_id, binding, *, disposition, synchronize=None, build=None, isolation=None,
                stop_after=None):
        if disposition == 'continue':
            return self.deliver(run_id, round_id, binding, synchronize=synchronize, build=build, stop_after=stop_after)
        if disposition == 'isolate_committed':
            with _exclusive_lock(self.lock):
                value = self.status(run_id, round_id)
                self._check(value, binding, 'release')
                value = self._recover_head(value)
                active = window(self.vault)
                if active and active['state'] not in TERMINAL and active['delivery_id'] != value['delivery_id']:
                    fail('DELIVERY_BUSY', 'another delivery owns the Vault')
                permitted = set(value['authorization'].get('isolation_pages', []))
                selected = {row['page_id'] for row in isolation or []}
                if not selected or not selected <= permitted or len(selected) != len(isolation):
                    fail('ACCESS_DENIED', 'forward isolation exceeds the fixed delivery authorization')
                from .short_build import content_isolation, record_content_isolation
                run = _load_json(self.build._run_path(value['build_run_id']))
                if value.get('isolation'):
                    pointer, rows = content_isolation(self.build, run)
                    if pointer != value['isolation'] or rows != isolation:
                        fail('IDEMPOTENCY_CONFLICT', 'forward isolation differs from the retained exact reviewed scope')
                elif (value['state'] not in ('build_committed', 'release_applying')
                        or value.get('release_id') and self.finalize._plan_path(value['release_id']).exists()):
                    fail('INVALID_STATE', 'forward isolation requires a committed Build before release planning')
                else:
                    self._base(value['base_release'], value['base_release_sha256'])
                    self._target_prefix(value, completed=True)
                    with self._writing(value):
                        pointer = record_content_isolation(self.finalize, run, isolation, value['authorization'])
                    value = self._persist(value, 'build_committed', isolation=pointer,
                                          owner={'task_id': binding['task_id'], 'binding_sha256': fingerprint(binding)})
            return self.deliver(run_id, round_id, binding, synchronize=synchronize, build=build, stop_after=stop_after)
        if disposition != 'abort_uncommitted':
            fail('INVALID_SCOPE', 'unknown incremental delivery recovery disposition')
        with _exclusive_lock(self.lock):
            value = self.status(run_id, round_id)
            self._check(value, binding, 'release')
            value = self._recover_head(value)
            if value['state'] == 'aborted':
                return value
            active = window(self.vault)
            if active and active['state'] not in TERMINAL and active['delivery_id'] != value['delivery_id']:
                fail('DELIVERY_BUSY', 'another delivery owns the Vault')
            if value['state'] not in ('prepared', 'build_committing'):
                fail('INVALID_STATE', 'committed domain changes require forward recovery')
            self._base(value['base_release'], value['base_release_sha256'])
            if self.build._registry()['revision'] != value['registry_revision']:
                fail('INVALID_STATE', 'identity registry changed after delivery preparation')
            from .knowledge_build import BUILD_ROOT
            for target in value['targets']:
                canonical = _vault_path(self.vault, target['path'])
                revision = _vault_path(self.vault, f"{BUILD_ROOT}/page-revisions/{target['page_id']}/{target['revision_id']}.json")
                if revision.exists() or (canonical.exists() and _sha(canonical.read_bytes()) == target['authored_sha256']):
                    fail('INVALID_STATE', 'delivery already contains domain writes and cannot be aborted')
            return self._persist(value, 'aborted',
                                 owner={'task_id': binding['task_id'], 'binding_sha256': fingerprint(binding)})
