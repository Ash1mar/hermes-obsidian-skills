"""One explicit Build policy switch at a held, accepted checkpoint_1."""
import copy
import hashlib
import json
import os
import re

from hermes_source_units import ContractError
from hermes_source_units.build_acceptance import (ADOPTION, MARKER, rebase_pause_evidence,
    verify_policy_adoption, checkpoint_batch_refresh)
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import fingerprint, record_validation_scope, validate_record


def fail(code, message):
    raise ContractError(code, '$', message)


def target_template():
    from orchestration import worker_pack
    return next(p for p in worker_pack() if p['kind'] == 'build-finalize')


def native_inventory(adapter, workflow):
    from native_stop_inventory import workflow_processes
    board = workflow['kanban']['board_id']
    if not board:
        fail('RUNTIME_UNVERIFIED', 'Build adoption requires the current native board')
    if workflow_processes(workflow['workflow_id'], board):
        fail('WORKER_ACTIVE', 'native workers or reconciler still consume this workflow')
    rows = json.loads(adapter.kanban.command('kanban', '--board', board, 'list', '--archived', '--json'))
    rows = rows.get('tasks', []) if isinstance(rows, dict) else rows
    downstream = {'build-finalize', 'vault-finalize-plan', 'checkpoint-2-validate',
                  'checkpoint-2-gate', 'release-apply', 'provider-sync', 'acceptance'}
    if any(c['node'] in downstream for c in workflow['kanban']['task_map']):
        fail('ADOPTION_NOT_ALLOWED', 'downstream bindings must not exist at checkpoint_1')
    for row in rows:
        if row['status'] in ('ready', 'running', 'triage', 'todo'):
            fail('WORKER_ACTIVE', 'native board contains runnable work')
        body = row.get('body') or {}
        if isinstance(body, str):
            try: body = json.loads(body)
            except ValueError: body = {}
        node = body.get('node', row.get('title')) if isinstance(body, dict) else row.get('title')
        if node not in downstream:
            continue
        snapshot = adapter.kanban.task_snapshot_fresh(board, row['id'])
        if row.get('session_id') or any(not (
                r.get('status') == 'scheduled' and r.get('outcome') == 'scheduled'
                and not r.get('worker_pid') and r.get('started_at') == r.get('ended_at')
                ) for r in snapshot.get('runs', [])):
            fail('ADOPTION_NOT_ALLOWED', 'downstream native execution already exists')
    return {'actual_consumers': 0, 'runnable_cards': 0, 'native_board': board,
            'rows_checked': len(rows), 'states_sha256': fingerprint(sorted([r['id'], r['status']] for r in rows))}


def adopt_build_acceptance(adapter, request, *, dry_run=False):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('ACCESS_DENIED', 'only the trusted operator may adopt a Build policy')
    service = adapter.workflow
    service._check_request(request)
    aid = request.get('adoption_id')
    if (not isinstance(aid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', aid)
            or request.get('adoption_scope') != ADOPTION or not str(request.get('reason', '')).strip()):
        fail('INVALID_SCHEMA', 'adoption needs an exact scope, stable ID and reason')
    wid = request['workflow_id']
    lock = _vault_path(service.vault, f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')
    with _exclusive_lock(lock), _exclusive_lock(service._lock(wid)), record_validation_scope():
        value = service.status(wid)
        if value['actor'] != request['actor']:
            fail('ACTOR_MISMATCH', 'adoption actor differs from workflow owner')
        prior = value.get('build_acceptance_adoption')
        if prior:
            if prior['adoption_id'] != aid or prior['input_digest'] != request['input_digest']:
                fail('IDEMPOTENCY_CONFLICT', 'Build policy was already adopted by another request')
            verify_policy_adoption(service, value)
            return {'ok': True, 'applied': False, 'replayed': True, 'workflow_id': wid,
                    'revision': value['revision'], 'report_ref': prior['report_ref'],
                    'background_dispatch': False, 'resumed': False, 'pause_released': False}
        value = service._mutation(request)
        control = value.get('pause_control', {})
        if (value['state'] != 'checkpoint_1' or value['current_stage'] != 'checkpoint_1'
                or value['cancel_requested'] or control.get('boundary') != 'checkpoint_1'
                or value['checkpoints']['checkpoint_1']['state'] != 'approved'
                or request.get('evidence_digest') != control.get('evidence_digest')):
            fail('INVALID_TRANSITION', 'requires the exact accepted and latched checkpoint_1 pause')
        service.assert_execution_ready(value)
        service.pinned_templates(value)
        previous = next(p for p in value['template_pins'] if p['kind'] == 'build-finalize')
        template = target_template()
        digest = 'sha256:'+hashlib.sha256(template['content'].encode('utf-8')).hexdigest()
        if (request.get('previous_pin_digest') != 'sha256:'+fingerprint(previous)
                or request.get('target_template_hash') != digest):
            fail('STALE_INPUT', 'observed previous or installed target Build template differs')
        if (MARKER not in template['content'] or '<!-- hermes-fixed-semantic/v1 -->' not in template['content']
                or MARKER in service.pinned_templates(value)['build-finalize']):
            fail('ADOPTION_NOT_ALLOWED', 'only legacy-to-program Build policy adoption is supported')
        prefix = f'_system/ledgers/ingest-workflows/{wid}/'
        root = _vault_path(service.vault, prefix)
        for folder in ('semantic-calls', 'output-attempts'):
            for path in (root/folder).glob('*.json'):
                saved = _load_json(path)
                if saved.get('phase') == 'page-review' or saved.get('node') == 'build-finalize':
                    fail('ADOPTION_NOT_ALLOWED', 'existing Build calls/journals need their original contract and budget')
        native = native_inventory(adapter, value)
        batch = service.knowledge._batch(value['batch_id'])
        with _exclusive_lock(service.knowledge._batch_lock_path(value['batch_id'])):
            if batch['state'] != 'checkpoint_1' or batch['cancel_requested']:
                fail('INVALID_TRANSITION', 'batch is not held at checkpoint_1')
            if any(s['state'] == 'leased' or s['lease']['worker_id'] for s in service.knowledge._slices(value['batch_id'])):
                fail('WORKER_ACTIVE', 'Pass leases must be empty')
            for run_id in batch['run_ids']:
                run = _load_json(service.knowledge._run_path(run_id))
                if (run['state'] != 'draft' or run['reviews']
                        or any(p['state'] != 'draft' or p['review'] is not None for p in run['page_revisions'])):
                    fail('ADOPTION_NOT_ALLOWED', 'Build domain work already started')
            old_path = _vault_path(service.vault, control['report_ref'])
            old_report = _load_json(old_path)
            evidence = service._pause_evidence(value, 'checkpoint_1')
            if (old_report.get('contract') != 'hermes-ingest-pause/v1'
                    or old_report.get('evidence_digest') != control['evidence_digest']
                    or 'sha256:'+fingerprint(old_report['evidence']) != control['evidence_digest']):
                fail('STALE_INPUT', 'checkpoint evidence changed before adoption')
            refresh = (checkpoint_batch_refresh(service,value,old_report['evidence'],evidence)
                       if old_report['evidence'] != evidence else None)
            target = {'kind': 'build-finalize', 'template_id': template['template_id'], 'template_hash': digest,
                      'path': prefix+'templates/build-finalize-'+digest[7:]+'.md'}
            new_evidence = rebase_pause_evidence(old_report['evidence'], previous, target, refresh)
            new_report = {'contract': 'hermes-ingest-pause/v1', 'evidence_digest': 'sha256:'+fingerprint(new_evidence),
                          'evidence': new_evidence}
            pause = {**copy.deepcopy(control), 'evidence_digest': new_report['evidence_digest'],
                     'report_ref': prefix+'pauses/checkpoint_1-build-policy-'+aid+'.json'}
            ref = prefix+'build-acceptance-adoptions/'+aid+'.json'
            report = {'contract': ADOPTION, 'request': dict(request), 'batch_id': value['batch_id'],
                      'previous_pin': previous, 'target_pin': target, 'previous_pause': control, 'new_pause': pause,
                      'previous_pause_sha256': hashlib.sha256(old_path.read_bytes()).hexdigest(),
                      'new_pause_sha256': hashlib.sha256(_json_bytes(new_report)).hexdigest(), 'native_inventory': native,
                      'domain_evidence_unchanged': True, 'resumed': False, 'pause_released': False}
            if refresh:report['batch_metadata_refresh']=refresh
            updated = copy.deepcopy(value)
            updated['template_pins'] = [target if p['kind'] == 'build-finalize' else p for p in value['template_pins']]
            updated['pause_control'] = pause
            updated['build_acceptance_adoption'] = {'adoption_id': aid, 'input_digest': request['input_digest'],
                'report_ref': ref, 'report_sha256': hashlib.sha256(_json_bytes(report)).hexdigest()}
            updated['revision'] += 1
            validate_record('ingest_workflow', updated)
            result = {'ok': True, 'applied': not dry_run, 'workflow_id': wid, 'revision': updated['revision'],
                      'previous_pin': previous, 'target_pin': target, 'pause': pause,
                      'report_ref': ref, 'background_dispatch': False, 'resumed': False, 'pause_released': False}
            if dry_run:
                return {**result, 'revision': value['revision']}
            payloads = [(target['path'], template['content'].encode('utf-8')),
                        (pause['report_ref'], _json_bytes(new_report)), (ref, _json_bytes(report))]
            for path, data in payloads:
                file = _vault_path(service.vault, path)
                if file.exists() and file.read_bytes() != data:
                    fail('IDEMPOTENCY_CONFLICT', 'immutable adoption payload differs')
            for path, data in payloads:
                file = _vault_path(service.vault, path)
                if not file.exists():
                    _write_atomic(file, data)
            verify_policy_adoption(service, updated)
            if service._pause_evidence(updated, 'checkpoint_1') != new_evidence:
                fail('STALE_INPUT', 'domain evidence changed during the policy switch')
            # The workflow pointer is the only authoritative mutable write.
            service._write(updated)
            return result
