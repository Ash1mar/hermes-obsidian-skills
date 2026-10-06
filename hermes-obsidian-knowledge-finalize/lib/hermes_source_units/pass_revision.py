"""Append-only semantic revisions under a held operator Pass boundary."""
from __future__ import annotations

import copy
import hashlib
import os
import re

from .source_units import _exclusive_lock, _load_json, _vault_path, _write_atomic, _json_bytes
from .validation import ContractError, fingerprint, validate_record


def fail(code, message):
    raise ContractError(code, '$', message)


class PassRevisionMixin:
    def revision_allows(self, value, node):
        revision = value.get('pass_revision')
        return bool(revision and revision['state'] == 'running'
                    and node.startswith('pass-slice:')
                    and node.partition(':')[2] in revision['slice_ids']
                    and value.get('pause_control', {}).get('boundary') == revision['boundary'])

    def revision_instructions(self, value):
        revision = value.get('pass_revision')
        if not revision:
            return None
        data = _vault_path(self.vault, revision['template_ref']).read_bytes()
        if hashlib.sha256(data).hexdigest() != revision['template_sha256']:
            fail('TEMPLATE_CHANGED', 'revision worker template changed')
        return data.decode('utf-8')

    def _verify_pause_snapshot(self, value):
        control = value['pause_control']
        evidence = self._pause_evidence(value, control['boundary'])
        report = _load_json(_vault_path(self.vault, control['report_ref']))
        if (evidence != report['evidence'] or 'sha256:' + fingerprint(evidence) != control['evidence_digest']
                or report['evidence_digest'] != control['evidence_digest']):
            fail('STALE_INPUT', 'current pause evidence changed')
        return evidence

    def revise_passes(self, request):
        """Authorize selected completed citation tasks; never release the pause."""
        self._check_request(request)
        if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
            fail('ACCESS_DENIED', 'workers cannot authorize revisions')
        revision_id = request.get('revision_id', '')
        if not isinstance(revision_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', revision_id):
            fail('INVALID_SCHEMA', 'revision requires a stable revision_id')
        with _exclusive_lock(self._lock(request['workflow_id'])):
            value = self._load(request['workflow_id'])
            if value['actor'] != request['actor']:
                fail('ACTOR_MISMATCH', 'revision actor differs')
            for old in [*value.get('pass_revision_history', []), *([value['pass_revision']] if value.get('pass_revision') else [])]:
                if old['revision_id'] == revision_id:
                    if old['input_digest'] != request['input_digest']:
                        fail('IDEMPOTENCY_CONFLICT', 'revision ID already used')
                    if _load_json(_vault_path(self.vault, old['request_ref'])) != dict(request):
                        fail('SOURCE_CHANGED', 'revision request evidence changed')
                    return value
            value = self._mutation(request)
            if value.get('pass_revision') and value['pass_revision']['state'] == 'running':
                fail('INVALID_TRANSITION', 'previous revision must be reviewed before another revision')
            boundary = value.get('pause_control', {}).get('boundary')
            if (boundary not in ('canary', 'pass') or value['current_stage'] != 'analyzing'
                    or value['cancel_requested'] or request.get('evidence_digest') != value['pause_control']['evidence_digest']):
                fail('INVALID_TRANSITION', 'revision requires the current held canary/pass pause')
            evidence = self._verify_pause_snapshot(value)
            batch = self.knowledge._batch(value['batch_id'])
            if batch['run_ids'] or batch.get('resource_reduction_ids') or batch.get('global_reduction_id'):
                fail('INVALID_TRANSITION', 'revision cannot change already consumed Pass evidence')
            raw = request.get('tasks', [])
            if not raw or len({item['task_id'] for item in raw}) != len(raw):
                fail('INVALID_SCHEMA', 'revision requires unique selected tasks')
            refs = request.get('evidence_refs', [])
            if not refs or not request.get('reason', '').strip():
                fail('ARTIFACT_REQUIRED', 'revision needs a reason and persisted review evidence')
            evidence_hashes = {ref: hashlib.sha256(_vault_path(self.vault, ref).read_bytes()).hexdigest() for ref in refs}
            tasks, selected = [], set()
            slices = self.knowledge._slices(value['batch_id'])
            if any(item['state'] == 'leased' for item in slices):
                fail('INVALID_TRANSITION', 'revision requires no active slice lease')
            for item in raw:
                tid = item['task_id']
                if tid not in batch['task_ids'] or not item.get('reason', '').strip():
                    fail('INVALID_SCOPE', 'revision task must belong to this batch and name a reason')
                matches = [s for s in slices if tid in s['task_ids']]
                if len(matches) != 1 or matches[0]['state'] != 'completed':
                    fail('INVALID_TRANSITION', 'revision requires completed slice evidence')
                sid = matches[0]['slice_id']
                if boundary == 'canary' and sid not in value['dispatch_policy']['slice_ids']:
                    fail('ACCESS_DENIED', 'revision task is outside the held canary')
                _, records = self.knowledge._pass_index([tid])
                passes = records.get(tid, [])
                if not passes or [p['sequence'] for p in passes] != list(range(len(passes))):
                    fail('INCOMPLETE_PASSES', 'revision requires contiguous Pass history')
                prior = passes[-1]
                if prior['pass_kind'] != 'citation' or prior['pass_id'] != item['pass_id']:
                    fail('STALE_INPUT', 'revision must pin the latest citation Pass')
                task = self.knowledge._task(tid)
                if task['status'] != 'running' or task['actor'] != value['actor']:
                    fail('INVALID_TRANSITION', 'revision cannot reopen consumed or foreign tasks')
                self.knowledge._live_units(task['target_refs'], value['actor'], batch['document_registry_revision'])
                prior_ref = next(ref for ref in matches[0]['result_refs'] if prior['pass_id'] in ref)
                tasks.append({'task_id': tid, 'pass_id': prior['pass_id'], 'pass_ref': prior_ref,
                              'sequence': prior['sequence'] + 1, 'reason': item['reason'],
                              'reading_package_id': prior['reading_package_id']})
                selected.add(sid)
            template = request.get('worker_template', '')
            if not isinstance(template, str) or not template.strip():
                fail('TEMPLATE_UNAVAILABLE', 'revision requires a pinned worker contract')
            root = f"_system/ledgers/ingest-workflows/{value['workflow_id']}/pass-revisions/{revision_id}"
            payloads = {'request.json': _json_bytes(dict(request)), 'before.json': _json_bytes(dict(value)),
                        'worker.md': template.encode('utf-8')}
            for name, data in payloads.items():
                path = _vault_path(self.vault, root + '/' + name)
                if path.exists() and path.read_bytes() != data:
                    fail('IDEMPOTENCY_CONFLICT', 'revision audit path differs')
                _write_atomic(path, data)
            if value.get('pass_revision'):
                value.setdefault('pass_revision_history', []).append(copy.deepcopy(value['pass_revision']))
            value['pass_revision'] = {'revision_id': revision_id, 'input_digest': request['input_digest'],
                'request_ref': root + '/request.json', 'before_ref': root + '/before.json',
                'template_ref': root + '/worker.md', 'template_sha256': hashlib.sha256(template.encode('utf-8')).hexdigest(),
                'evidence_hashes': [{'ref': ref, 'sha256': digest} for ref, digest in evidence_hashes.items()], 'boundary': boundary, 'state': 'running',
                'tasks': tasks, 'slice_ids': sorted(selected), 'original_pause': copy.deepcopy(value['pause_control']),
                'protected_files': [{'ref': ref, 'sha256': digest} for ref, digest in evidence['files'].items()
                    if not any(ref == self.knowledge._slice_path(value['batch_id'], sid).relative_to(self.vault).as_posix()
                               for sid in selected)
                    and not any(ref == self.knowledge._task_path(item['task_id']).relative_to(self.vault).as_posix() for item in tasks)
                    and ref != self.knowledge._batch_path(value['batch_id']).relative_to(self.vault).as_posix()]}
            value['revision'] += 1
            self._write(value)
            return value

    def prepare_pass_revision(self, value):
        """Idempotently reopen only selected completed slices after authorization."""
        revision = value.get('pass_revision')
        if not revision or revision['state'] != 'running':
            return
        self.revision_instructions(value)
        batch_id = value['batch_id']
        with _exclusive_lock(self.knowledge._batch_lock_path(batch_id)):
            for sid in revision['slice_ids']:
                item = self.knowledge._slice(batch_id, sid)
                if item['state'] == 'completed':
                    pending = [t for t in revision['tasks'] if t['task_id'] in item['task_ids']
                               and not any(p.get('revision_id') == revision['revision_id']
                                   for p in self.knowledge._pass_index([t['task_id']])[1].get(t['task_id'], []))]
                    if pending:
                        item.update(state='ready', retry_at=None, last_error=None, revision=item['revision'] + 1)
                        self.knowledge._clear_lease(item)
                        self.knowledge._write_slice(item)

    def finish_pass_revision(self, request):
        """Refresh evidence under the same pause; leave semantic acceptance to reviewer."""
        with _exclusive_lock(self._lock(request['workflow_id'])):
            value = self._mutation(request)
            revision = value.get('pass_revision')
            if not revision or revision['state'] != 'running':
                return value
            if not all(self.knowledge._slice(value['batch_id'], sid)['state'] == 'completed' for sid in revision['slice_ids']):
                return value
            for task in revision['tasks']:
                records = self.knowledge._pass_index([task['task_id']])[1].get(task['task_id'], [])
                current = next((p for p in records if p['sequence'] == task['sequence']), None)
                if not current or current.get('revision_id') != revision['revision_id'] or current.get('supersedes_pass_id') != task['pass_id']:
                    fail('INCOMPLETE_COVERAGE', 'revision needs a new citation for each selected task')
            for item in [*revision['protected_files'], *revision['evidence_hashes']]:
                ref, digest = item['ref'], item['sha256']
                if hashlib.sha256(_vault_path(self.vault, ref).read_bytes()).hexdigest() != digest:
                    fail('SOURCE_CHANGED', 'revision changed protected or review evidence')
            evidence = self._pause_evidence(value, revision['boundary'])
            digest = 'sha256:' + fingerprint(evidence)
            root = str(_vault_path(self.vault, revision['request_ref']).parent.relative_to(self.vault).as_posix())
            ref = root + '/pause.json'
            report = {'contract': 'hermes-ingest-pause/v1', 'evidence_digest': digest, 'evidence': evidence}
            _write_atomic(_vault_path(self.vault, ref), _json_bytes(report))
            value['pause_control'].update(report_ref=ref, evidence_digest=digest)
            revision['state'] = 'awaiting_review'
            value['revision'] += 1
            self._write(value)
            return value

    def accept_pass_revision(self, request):
        self._check_request(request)
        if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
            fail('ACCESS_DENIED', 'workers cannot accept revisions')
        with _exclusive_lock(self._lock(request['workflow_id'])):
            value = self._load(request['workflow_id'])
            if value['actor'] != request['actor']:
                fail('ACTOR_MISMATCH', 'review actor differs')
            revision = value.get('pass_revision')
            if revision and revision['state'] == 'accepted' and request.get('revision_id') == revision['revision_id']:
                ref = str(_vault_path(self.vault, revision['request_ref']).parent.relative_to(self.vault).as_posix()) + '/accept.json'
                if _load_json(_vault_path(self.vault, ref)) != dict(request):
                    fail('IDEMPOTENCY_CONFLICT', 'accepted revision request differs')
                return value
            value = self._mutation(request)
            revision = value.get('pass_revision')
            if not revision or revision['state'] != 'awaiting_review' or request.get('revision_id') != revision['revision_id']:
                fail('INVALID_TRANSITION', 'accept requires current completed revision')
            if request.get('evidence_digest') != value['pause_control']['evidence_digest']:
                fail('STALE_INPUT', 'review must pin current pause digest')
            self._verify_pause_snapshot(value)
            refs = request.get('evidence_refs', [])
            if not refs or not all(_vault_path(self.vault, ref).is_file() for ref in refs):
                fail('ARTIFACT_REQUIRED', 'accept needs explicit persisted reviewer evidence')
            ref = str(_vault_path(self.vault, revision['request_ref']).parent.relative_to(self.vault).as_posix()) + '/accept.json'
            _write_atomic(_vault_path(self.vault, ref), _json_bytes(dict(request)))
            revision['state'] = 'accepted'
            value['revision'] += 1
            self._write(value)
            return value
