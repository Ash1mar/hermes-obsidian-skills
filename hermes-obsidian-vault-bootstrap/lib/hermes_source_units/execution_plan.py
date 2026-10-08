"""Append-only execution amendments that preserve canonical tasks and outcomes."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections import Counter

from .source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from .validation import ContractError, fingerprint, validate_record
from .model_projection import RENDERER, BEGIN_ENVELOPE_RESERVE


def fail(code, message):
    raise ContractError(code, '$', message)


class ExecutionPlanMixin:
    def _execution_journal(self, wid):
        return _vault_path(self.vault, f'_system/ledgers/ingest-workflows/{wid}/execution-plan.pending.json')

    def execution_plan(self, workflow):
        pointer = workflow.get('execution_plan')
        if not pointer:
            return None
        plan = _load_json(_vault_path(self.vault, pointer['path']))
        body = {k: v for k, v in plan.items() if k != 'plan_hash'}
        if (fingerprint(body) != plan.get('plan_hash') or pointer['sha256'] != plan['plan_hash']
                or plan['workflow_id'] != workflow['workflow_id'] or plan['batch_id'] != workflow['batch_id']):
            fail('STALE_INPUT', 'execution manifest identity or fingerprint changed')
        return plan

    def assert_execution_ready(self, workflow):
        if self._execution_journal(workflow['workflow_id']).exists():
            fail('EXECUTION_PLAN_PENDING', 'finish the authorized amendment before resume/dispatch')
        plan = self.execution_plan(workflow)
        batch_ref = self.knowledge._batch(workflow['batch_id']).get('execution_plan_ref') if workflow['batch_id'] else None
        workflow_ref = workflow.get('execution_plan', {}).get('path')
        if batch_ref != workflow_ref:
            fail('EXECUTION_PLAN_PENDING', 'workflow and batch execution pointers disagree')

    def execution_template(self, workflow, slice_id):
        plan = self.execution_plan(workflow)
        while plan:
            if slice_id in plan['new_slice_ids']:
                path = _vault_path(self.vault, plan['template_ref'])
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != plan['template_hash']:
                    fail('TEMPLATE_CHANGED', 'execution template changed')
                return data.decode('utf-8')
            prior = plan.get('previous')
            if not prior:
                break
            plan = self.execution_plan({**workflow, 'execution_plan': prior})
        return None

    def execution_slice(self, workflow, slice_id):
        plan = self.execution_plan(workflow)
        while plan:
            if slice_id in plan['new_slice_ids']:
                return plan, plan['slice_inputs'][slice_id]
            if not plan.get('previous'):
                break
            plan = self.execution_plan({**workflow, 'execution_plan':plan['previous']})
        fail('STALE_INPUT', 'compact slice has no authorized execution generation')

    def _verify_execution_evidence(self, workflow):
        self.pinned_templates(workflow)
        for item in workflow['source_outcomes']:
            if hashlib.sha256(_vault_path(self.vault, item['path']).read_bytes()).hexdigest() != item['content_sha256']:
                fail('SOURCE_CHANGED', 'source bytes changed before execution amendment')

    def _execution_preview(self, workflow, request):
        service = self.knowledge
        service.source.enable_session_cache()
        batch = service._batch(workflow['batch_id'])
        slices = service._slices(workflow['batch_id'])
        if (workflow['state'] != 'cancelled' or not workflow['cancel_requested'] or
                workflow['current_stage'] != 'analyzing' or not batch['cancel_requested'] or
                batch['state'] != 'cancelled' or any(s['lease']['worker_id'] or s['state']=='leased' for s in slices)):
            fail('INVALID_TRANSITION', 'amendment requires a cancelled Pass batch without leases')
        if workflow.get('pass_revision') and workflow['pass_revision']['state'] != 'accepted':
            fail('REVISION_REVIEW_REQUIRED', 'finish revision review before changing execution')
        if workflow['dispatch_policy']['mode'] != 'full':
            fail('INVALID_TRANSITION', 'canary must already be accepted and released before amendment')
        self._verify_execution_evidence(workflow)
        config = service._slice_config({**batch['slice_config'], 'slice_max_tasks': request['max_tasks'],
                                      'slice_max_input_codepoints': request['max_input_codepoints']})
        completed = [s for s in slices if s['state'] == 'completed']
        completed_tasks = {tid for s in completed for tid in s['task_ids']}
        planned = [tid for s in slices for tid in s['task_ids']]
        if Counter(planned) != Counter(batch['task_ids']):
            fail('INCOMPLETE_COVERAGE', 'active slices do not own every task exactly once')
        protected = {}
        def protect(path):
            protected[path.relative_to(self.vault).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        groups, current, total, partial = [], [], BEGIN_ENVELOPE_RESERVE, []
        for tid in batch['task_ids']:
            task = service._task(tid)
            protect(service._task_path(tid))
            service._verify_planned_measurement(task, batch['actor'], batch['document_registry_revision'], None)
            root = _vault_path(self.vault, f'_system/knowledge-builds/task-{tid}')
            for folder in ('passes', 'readings'):
                for path in (root / folder).glob('*.json'):
                    protect(path)
            # Validate sequence chains and cryptographic identity of every old Pass.
            _, records = service._pass_index([tid])
            for record in records.get(tid, []):
                if fingerprint({k: v for k, v in record.items() if k != 'pass_id'}) != record['pass_id']:
                    fail('SOURCE_CHANGED', 'historical Pass fingerprint changed')
            if tid in completed_tasks:
                if not records.get(tid) or records[tid][-1]['sequence'] < 1:
                    fail('INCOMPLETE_COVERAGE', 'completed slice lacks durable citation')
                package = service._existing_reading_package(task, batch['actor'], batch['document_registry_revision'])
                if package is None:
                    fail('INCOMPLETE_COVERAGE', 'completed task lacks its bounded package')
                service.model_input(tid, package)  # fresh source/QA/provenance checks without a rerun
                continue
            if task['status'] not in ('pending', 'running'):
                fail('INVALID_STATE', 'uncompleted task is not resumable')
            projection = service.preview_model_input(tid, batch)
            descriptor = {'task':'t32', 'path':projection['path'], 'input_codepoints':projection['codepoints']}
            size = projection['codepoints'] + len(json.dumps(descriptor, ensure_ascii=False, indent=2)) + 32
            if size + BEGIN_ENVELOPE_RESERVE > config['slice_max_input_codepoints']:
                fail('READING_WINDOW_OVERSIZE', 'one compact task exceeds execution input budget')
            if records.get(tid):
                partial.append(tid)
            item = {'task_id':tid, 'codepoints':size, 'input_id':projection['input_id'],
                    'canonical_fingerprint':service._slice_task_input(batch, task, remeasure=True)['fingerprint']}
            if current and (len(current) >= config['slice_max_tasks'] or total+size > config['slice_max_input_codepoints']):
                groups.append(current); current, total = [], BEGIN_ENVELOPE_RESERVE
            current.append(item); total += size
        if current:
            groups.append(current)
        for value in completed:
            protect(service._slice_path(batch['batch_id'], value['slice_id']))
            for ref in value['result_refs']:
                protect(_vault_path(self.vault, ref))
        template = request['worker_template']
        template_hash = hashlib.sha256(template.encode('utf-8')).hexdigest()
        new_slices = []
        for ordinal, group in enumerate(groups, 1):
            digest = 'sha256:' + fingerprint({'amendment_id':request['amendment_id'], 'tasks':group,
                'config':config, 'renderer':RENDERER, 'template_hash':template_hash})
            sid = f'pass-amend-{ordinal:04d}-{digest[-12:]}'
            new_slices.append({'contract':'hermes-knowledge-build-slice/v1', 'slice_id':sid,
                'batch_id':batch['batch_id'], 'task_ids':[t['task_id'] for t in group],
                'input_codepoints':sum(t['codepoints'] for t in group) + BEGIN_ENVELOPE_RESERVE, 'input_fingerprint':digest,
                'template_id':RENDERER, 'template_hash':template_hash, 'state':'cancelled', 'attempt':0,
                'lease':{'worker_id':None,'claimed_at':None,'heartbeat_at':None,'expires_at':None},
                'retry_at':None, 'result_refs':[], 'last_error':None, 'reslice_count':0, 'revision':1})
        task_owner = [tid for s in [*completed, *new_slices] for tid in s['task_ids']]
        if Counter(task_owner) != Counter(batch['task_ids']):
            fail('INCOMPLETE_COVERAGE', 'amendment lost or duplicated task ownership')
        for value in new_slices:
            validate_record('knowledge_slice', value)
        summary = {'completed_slices_preserved':len(completed), 'partial_task_ids':partial,
            'task_ids_unchanged':True, 'old_slice_count':len(slices),
            'new_slice_count':len(completed)+len(new_slices), 'pending_slice_count':len(new_slices),
            'model_input_codepoints':sum(s['input_codepoints'] for s in new_slices),
            'pause_control_unchanged':True}
        return batch, completed, new_slices, groups, config, protected, summary

    def amend_execution(self, request, *, dry_run=False):
        if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
            fail('ACCESS_DENIED', 'workers cannot amend execution')
        self._check_request(request)
        wid, aid = str(request['workflow_id']), str(request['amendment_id'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', aid):
            fail('INVALID_SCHEMA', 'invalid amendment identity')
        if (not isinstance(request.get('worker_template'), str) or not request['worker_template'].strip()
                or not str(request.get('reason', '')).strip() or type(request.get('max_tasks')) is not int
                or not 1 <= request['max_tasks'] <= 32 or type(request.get('max_input_codepoints')) is not int
                or request['max_input_codepoints'] < 1):
            fail('INVALID_SCHEMA', 'amendment requires a template, reason and bounded input configuration')
        with _exclusive_lock(self._lock(wid)):
            workflow = self._load(wid)
            if workflow['actor'] != request['actor']:
                fail('ACTOR_MISMATCH', 'amendment actor differs')
            previous = self.execution_plan(workflow)
            historical = previous
            while historical and historical['amendment_id'] != aid:
                historical = self.execution_plan({**workflow,'execution_plan':historical['previous']}) if historical.get('previous') else None
            if historical and historical['amendment_id'] == aid and not self._execution_journal(wid).exists():
                if historical['request_digest'] != request['input_digest']:
                    fail('IDEMPOTENCY_CONFLICT', 'historical amendment identity already used')
                return {'ok':True,'applied':True,'workflow_id':wid,'revision':workflow['revision'],**historical['summary']}
            journal_path = self._execution_journal(wid)
            if journal_path.exists():
                if dry_run:
                    fail('EXECUTION_PLAN_PENDING', 'recover pending amendment first')
                journal = _load_json(journal_path)
                if journal['request_digest'] != request['input_digest']:
                    fail('IDEMPOTENCY_CONFLICT', 'another execution amendment is pending')
            else:
                workflow = self._mutation(request)
                with _exclusive_lock(self.knowledge._batch_lock_path(workflow['batch_id'])):
                    batch, completed, slices, groups, config, protected, summary = self._execution_preview(workflow, request)
                if dry_run:
                    return {'ok':True, 'applied':False, 'workflow_id':wid, 'revision':workflow['revision'], **summary}
                prefix = f'_system/ledgers/ingest-workflows/{wid}/execution-plans/{aid}'
                manifest = {'contract':'hermes-execution-plan/v1', 'workflow_id':wid,
                    'batch_id':workflow['batch_id'], 'amendment_id':aid, 'request_digest':request['input_digest'],
                    'previous':workflow.get('execution_plan'), 'renderer':RENDERER,
                    'reason':request['reason'], 'template_ref':prefix+'/worker.md',
                    'template_hash':hashlib.sha256(request['worker_template'].encode('utf-8')).hexdigest(),
                    'new_slice_ids':[s['slice_id'] for s in slices],
                    'slice_inputs':{s['slice_id']:group for s,group in zip(slices,groups)}, 'config':config,
                    'preserved_slice_ids':[s['slice_id'] for s in completed],
                    'protected':protected, 'summary':summary}
                manifest['plan_hash'] = fingerprint(manifest)
                manifest_ref = prefix+'/manifest.json'
                updated_batch = copy.deepcopy(batch)
                updated_batch.update(slice_ids=[s['slice_id'] for s in [*completed, *slices]],
                    slice_config=config, execution_plan_ref=manifest_ref, revision=batch['revision']+1,
                    last_operation='amend-execution')
                validate_record('knowledge_batch', updated_batch)
                updated_workflow = copy.deepcopy(workflow)
                # Obsolete cards retain their immutable audit bindings but cannot
                # receive writes. Completed canonical identities stay untouched.
                names = {'pass-slice:'+s['slice_id'] for s in completed}
                updated_workflow['kanban']['task_map'] = [c for c in workflow['kanban']['task_map'] if c['node'] in names]
                updated_workflow.update(execution_plan={'path':manifest_ref,'sha256':manifest['plan_hash']},
                                        revision=workflow['revision']+1)
                validate_record('ingest_workflow', updated_workflow)
                journal = {'request_digest':request['input_digest'], 'workflow_before':fingerprint(workflow),
                    'batch_before':fingerprint(batch), 'workflow':updated_workflow, 'batch':updated_batch,
                    'manifest':manifest, 'manifest_ref':manifest_ref, 'slices':slices,
                    'worker_template':request['worker_template']}
                journal['journal_hash'] = fingerprint(journal)
                _write_atomic(journal_path, _json_bytes(journal))
            if fingerprint({k:v for k,v in journal.items() if k != 'journal_hash'}) != journal.get('journal_hash'):
                fail('SOURCE_CHANGED', 'pending execution journal changed')
            self._verify_execution_evidence(workflow)
            manifest = journal['manifest']
            for ref, sha in manifest['protected'].items():
                if hashlib.sha256(_vault_path(self.vault, ref).read_bytes()).hexdigest() != sha:
                    fail('SOURCE_CHANGED', 'preserved evidence changed during amendment')
            self.knowledge.source.enable_session_cache()
            for sid in manifest['preserved_slice_ids']:
                preserved = self.knowledge._slice(manifest['batch_id'], sid)
                for tid in preserved['task_ids']:
                    task = self.knowledge._task(tid)
                    package = self.knowledge._existing_reading_package(
                        task, journal['batch']['actor'], journal['batch']['document_registry_revision'])
                    if package is None:
                        fail('INCOMPLETE_COVERAGE', 'preserved task lost its reading package')
                    self.knowledge.model_input(tid, package)
            for group in manifest['slice_inputs'].values():
                for expected in group:
                    observed = self.knowledge.preview_model_input(expected['task_id'], journal['batch'])
                    if observed['input_id'] != expected['input_id']:
                        fail('SOURCE_CHANGED', 'planned model evidence changed during interrupted amendment')
            if fingerprint(workflow) not in (journal['workflow_before'], fingerprint(journal['workflow'])):
                fail('REVISION_CONFLICT', 'workflow changed during interrupted amendment')
            with _exclusive_lock(self.knowledge._batch_lock_path(manifest['batch_id'])):
                current_batch = self.knowledge._batch(manifest['batch_id'])
                if fingerprint(current_batch) not in (journal['batch_before'], fingerprint(journal['batch'])):
                    fail('REVISION_CONFLICT', 'batch changed during interrupted amendment')
                for value in journal['slices']:
                    path = self.knowledge._slice_path(manifest['batch_id'], value['slice_id'])
                    if path.exists() and _load_json(path) != value:
                        fail('IDEMPOTENCY_CONFLICT', 'new execution slice changed')
                    self.knowledge._write_slice(value)
                for ref, data in ((manifest['template_ref'], journal['worker_template'].encode('utf-8')),
                                  (journal['manifest_ref'], _json_bytes(manifest))):
                    path = _vault_path(self.vault, ref)
                    if path.exists() and path.read_bytes() != data:
                        fail('IDEMPOTENCY_CONFLICT', 'execution artifact identity changed')
                    _write_atomic(path, data)
                _write_atomic(self.knowledge._batch_path(manifest['batch_id']), _json_bytes(journal['batch']))
                self._write(journal['workflow'])
                journal_path.unlink()
            return {'ok':True, 'applied':True, 'workflow_id':wid,
                    'revision':journal['workflow']['revision'], **manifest['summary']}
