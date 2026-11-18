#!/usr/bin/env python3
"""Run a new independent native short-path test through final acceptance."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

import run_short_evidence_test as runner
from post_mineru_test import BOOTSTRAP_SHORT, CONTRACT as IMPORT_TEST, bootstrap_source_boundary
from hermes_source_units.page_governance import POLICY, validate_retrieval_snapshot
from hermes_source_units.short_build import record_content_isolation
from hermes_source_units.source_units import _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import fingerprint
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from short_evidence_pass import require
from short_formal_build import delivery as publish, execute, final_plan, release_identity
from short_fragment_disposition import delivery_partition
from short_operator_control import DETACHED, NODES, PAUSED, OperatorControl


class SourceWaitDetached(RuntimeError):
    """Observation stopped without changing native workflow state."""


def source_binding(vault, request, receipt, workflow):
    """Authenticate the durable start before observing or consuming its output."""
    wid = 'ingest-' + request['operation_id']
    ref = f"_system/reports/vault-startup/{request['operation_id']}.json"
    identity = receipt['identity']
    require(receipt['contract'] == 'vault-startup/v1' and receipt['operation_id'] == request['operation_id']
            and receipt['vault'] == str(vault) and identity['request'] == request
            and receipt['input_digest'] == fingerprint(identity)
            and receipt['phase'] in ('import', 'sources_staged', 'workflow_created')
            and receipt.get('workflow_id', wid) == wid and receipt.get('bootstrap_ref', ref) == ref,
            'saved provisioning receipt does not match this request')
    report = _load_json(_vault_path(vault, ref))
    actual = _load_json(_vault_path(vault, '_system/vault.json'))['vault']
    hashes = {f'10_Raw/{Path(path).name}': digest for path, digest in identity['source_hashes'].items()}
    scope = workflow['scope']
    start = {'workflow_id': wid, 'actor': request['actor'], 'profile': 'compact-5', 'scope': scope,
             'expected_revision': 0}
    require(report['contract'] == 'vault-startup-report/v1' and report['workflow_id'] == wid
            and report['input_digest'] == receipt['input_digest'] and report['vault_id'] == request['vault_id']
            and report['source_hashes'] == identity['source_hashes'] and actual['id'] == request['vault_id']
            and actual['name'] == request['vault_name'] and actual['security_domain'] == request['security_domain']
            and workflow['workflow_id'] == wid and workflow['actor'] == request['actor']
            and workflow['profile'] == start['profile'] and workflow['start_digest'] == 'sha256:' + fingerprint(start)
            and scope['bootstrap_ref'] == ref and scope['pause_after'] == ['exact_plan']
            and set(hashes) == set(scope['source_paths']), 'native workflow does not match the saved provisioning identity')
    return hashes


def wait_for_source(vault, request, ledger, receipt, update, previous_files, *, idle_seconds=1200):
    workflow = _load_json(ledger)
    hashes = source_binding(vault, request, receipt, workflow)
    wid = workflow['workflow_id']
    kb, kc, _ = runner.runtime()
    since = time.monotonic()
    signature = None
    node_for = {path: 'source-prepare:' + fingerprint({'path': path, 'content_sha256': digest})[:16]
                for path, digest in hashes.items()}
    observed = {row['path']: row for row in previous_files}
    while True:
        workflow = _load_json(ledger)
        kanban = workflow['kanban']
        board = kanban['board_id']
        tasks = {}
        if board:
            with closing(kc.connect(board=board)) as conn:
                tasks = {task.id: task for task in kb.list_tasks(conn)}
        processes = runner.workflow_processes(wid, board)
        workers = {row['task_id'] for row in processes if row['kind'] == 'native_worker' and row['task_id'] in tasks}
        by_node = {row['node']: row['task_id'] for row in kanban['task_map']}
        outcomes = {row['path']: row for row in workflow['source_outcomes']}
        files = []
        for path in workflow['scope']['source_paths']:
            task_id = by_node.get(node_for[path])
            task = tasks.get(task_id)
            prior = observed.get(path, {})
            row = {'path': path, 'task_id': task_id, 'native_status': task.status if task else None}
            if path in outcomes:
                row['status'] = outcomes[path]['status']
            elif task_id in workers:
                row['status'] = 'running'
            elif task and task.status in ('ready', 'todo', 'triage', 'running'):
                row['status'] = 'queued' if task.status in ('ready', 'todo', 'triage') else 'awaiting_reconciliation'
            else:
                row['status'] = 'waiting'
            run_id = task.current_run_id if task else None
            row['run_id'] = run_id
            if run_id is not None and prior.get('run_id') == run_id and 'observed_running_at' in prior:
                row['observed_running_at'] = prior['observed_running_at']
            elif row['status'] == 'running':
                row['observed_running_at'] = time.time()
            files.append(row)
            observed[path] = row
        stage = 'exact' if workflow['current_stage'] in ('planning', 'analyzing') else 'prepare'
        native = tuple(sorted((task.id, task.status, task.current_run_id) for task in tasks.values()))
        current = (workflow['revision'], native, frozenset(workers))
        busy = any(task.status == 'running' or task.claim_lock for task in tasks.values())
        if current != signature:
            since = time.monotonic()
            signature = current
            update(stage=stage, state='waiting', revision=workflow['revision'], board=board, files=files,
                   active_tasks=len(workers), native_state=workflow['state'], last_progress_at=time.time())
        require(not workflow['cancel_requested'] and workflow['state'] not in ('failed', 'cancelled', 'partial', 'completed'),
                'native workflow ended before the exact-plan source handoff: ' + workflow['state'])
        if workflow.get('pause_control', {}).get('boundary') == 'exact_plan' and not busy and not processes:
            return workflow
        # Worker/supervisor deadlines own queue and conversion time. The observer
        # only expires an idle wait, and can be reattached to the same workflow.
        no_consumer = not processes and not any(task.status in ('ready', 'todo', 'triage', 'running')
                                               or task.claim_lock for task in tasks.values())
        elapsed = time.monotonic() - since
        if not workers and (elapsed >= idle_seconds or (no_consumer and elapsed >= min(30, idle_seconds))):
            raise SourceWaitDetached('source handoff is pending without observed native progress; reconnect to the same workflow')
        time.sleep(2)


def prepare(vault, request, root, provisioner, bootstrap, *, attach=False, checkpoint=None):
    wid = 'ingest-' + request['operation_id']
    fresh = request['test_mode'] == BOOTSTRAP_SHORT
    require(request['test_mode'] in (BOOTSTRAP_SHORT, IMPORT_TEST) and request['pause_after'] == ['exact_plan']
            and request['retrieval_policy']['sync_enabled'] and not request['retrieval_policy']['query_enabled'],
            'independent short test requires an isolated exact-plan source boundary and scoped Provider')
    path = root / 'provisioning-request.json'
    receipt = root / 'provisioning-receipt.json'
    ledger = _vault_path(vault, f'_system/ledgers/ingest-workflows/{wid}.json')
    status_path = root / 'source-wait.json'
    previous = _load_json(status_path) if status_path.is_file() else {}
    start = time.time()
    status = {'workflow_id': wid, 'stage': 'bootstrap', 'state': 'waiting',
              'started_at': start, 'last_progress_at': start}

    def update(**fields):
        status.update(updated_at=time.time(), **fields)
        _write_atomic(status_path, _json_bytes(status))

    def provision(command):
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=180)
        require(result.returncode == 0, 'native source provisioning entry failed: ' + result.stderr[-2500:])

    try:
        if attach:
            require(path.is_file() and receipt.is_file() and ledger.is_file() and _load_json(path) == request,
                    'attachment requires the original provisioning request, receipt and native workflow')
        else:
            require(not ledger.exists(), 'native workflow already exists; attach to its existing ledger')
            runner.save(path, request)
            update()
            entry = 'start_vault_workflow.py' if fresh else 'start_post_mineru_test.py'
            cmd = [sys.executable, '-B', str(provisioner / 'scripts' / entry),
                   '--request', str(path), '--record', str(receipt), '--bootstrap-skill', str(bootstrap)]
            if fresh and checkpoint:
                if not receipt.is_file() or _load_json(receipt).get('phase') != 'sources_staged':
                    provision([*cmd, '--prepare-only'])
                staged = _load_json(receipt)
                report = _vault_path(vault, staged['bootstrap_ref'])
                update(stage='prepare')
                if checkpoint('prepare', {'startup_sha256': runner.sha(receipt), 'report_sha256': runner.sha(report)}):
                    return None
            else:
                update(stage='prepare')
            provision(cmd)
        workflow = wait_for_source(vault, request, ledger, _load_json(receipt), update, previous.get('files', []))
        update(stage='exact')
        handoff = source_handoff(vault, wid, workflow, root, fresh, start)
    except SourceWaitDetached as exc:
        update(state='detached', error=str(exc))
        raise
    except Exception as exc:
        update(state='failed', error=str(exc))
        raise
    update(state='handoff', last_progress_at=time.time())
    if checkpoint and checkpoint('exact', {'handoff_sha256': runner.sha(root / 'source-handoff.json')}):
        return None
    return handoff


def source_handoff(vault, wid, workflow, root, fresh, start):
    if fresh:
        pins = bootstrap_source_boundary(vault, wid, workflow)
        require(pins, 'native bootstrap source handoff is missing')
        metadata = {'fresh_converted_sources': len(pins), 'imported_files': 0,
                    'source_outcomes': workflow['source_outcomes']}
    else:
        startup = _load_json(_vault_path(vault, workflow['scope']['bootstrap_ref']))
        imported = _load_json(_vault_path(vault, startup['test_bundle_import']['ref']))
        ready = {r['path']: r for r in workflow['source_outcomes'] if r['status'] == 'ready'}
        pins = []
        for entry in imported['sources']:
            outcome = ready[entry['raw_ref']]
            manifest = entry['bundle_ref'] + '/manifest.json'
            require(manifest in outcome['artifact_refs'], 'source handoff lost imported Bundle identity')
            pins.append({'original': entry['raw_ref'], 'resource_id': outcome['resource_id'],
                         'unit_set_id': outcome['unit_set_id'], 'bundle_manifest': manifest})
        metadata = {'import_receipt': startup['test_bundle_import'], 'mineru_calls': imported['mineru_calls'],
                    'imported_files': sum(len(s['origin_files']) for s in imported['sources'])}
    runner.source_boundary(vault, wid, pins)
    handoff = {'workflow_id': wid, 'revision': workflow['revision'], 'pause': workflow['pause_control'],
               'pins': pins, 'seconds': time.time() - start, **metadata}
    path = root / 'source-handoff.json'
    if path.is_file():
        saved = _load_json(path)
        require({k: v for k, v in saved.items() if k != 'seconds'} ==
                {k: v for k, v in handoff.items() if k != 'seconds'}, 'saved source handoff changed')
        return saved
    runner.save(path, handoff)
    return handoff


def repeat_handoff(vault, request_path, previous):
    marker = _load_json(previous / 'pipeline-start.json')
    require(marker['request_sha256'] == runner.sha(request_path),
            'another round requires the exact same provisioning request')
    handoff = _load_json(previous / 'source-handoff.json')
    qualification = _load_json(previous / 'source-qualification.json')
    from post_mineru_test import verify_import_board

    workflow = _load_json(_vault_path(vault, f"_system/ledgers/ingest-workflows/{handoff['workflow_id']}.json"))
    require(workflow['pause_control'] == handoff['pause'] and qualification['ok']
            and qualification['resolved'] == len(handoff['pins']),
            'the source handoff or its qualification changed')
    runner.source_boundary(vault, handoff['workflow_id'], handoff['pins'])
    verify_import_board(workflow['kanban']['board_id'])
    return handoff, qualification


def synchronize(vault, root, release_id, provider_config):
    base = Path(__file__).resolve().parents[2]
    finalize = base / 'hermes-obsidian-knowledge-finalize'
    pythonpath = os.pathsep.join((str(finalize / 'lib'), str(base / 'qmd-like-rag/src')))
    env = {**os.environ, 'PYTHONPATH': pythonpath, 'PYTHONDONTWRITEBYTECODE': '1'}
    before = time.monotonic()
    cmd = [sys.executable, '-B', str(finalize / 'scripts/sync_release_index.py'),
           str(vault), '--provider-config', str(provider_config)]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding='utf-8', timeout=1900)
    result = json.loads(proc.stdout)
    runner.save(root / 'provider-result.json', {**result, 'seconds': time.monotonic() - before,
                                                'returncode': proc.returncode})
    index = result['index']
    require(proc.returncode == 0 and index['status'] == 'ready' and index['release_id'] == release_id,
            'Provider did not commit the exact new release')
    return index


def qualify_sources(vault, root, handoff, request):
    """Use the existing role-labelled, hash-bound source resolution."""
    result_path = root / 'source-qualification.json'
    if result_path.is_file():
        return _load_json(result_path)
    scripts = Path(__file__).resolve().parents[2] / 'hermes-obsidian-controlled-ingest/scripts'
    sys.path.insert(0, str(scripts))
    from governance_repository import JsonGovernanceRepository
    from source_governance_resolution import digest

    repo = JsonGovernanceRepository(vault)
    wid = handoff['workflow_id']
    workflow_ref = f'_system/ledgers/ingest-workflows/{wid}.json'
    workflow = _load_json(_vault_path(vault, workflow_ref))
    path = root / 'source-qualification-request.json'
    if path.is_file():
        resolution = _load_json(path)
    else:
        org, registry = repo.load_state()
        resolution = {'workflow_id': wid, 'workflow_ref': workflow_ref, 'actor': request['actor'],
                      'operation_id': request['operation_id'] + '-short-source',
                      'reason': 'Authorized independent short test; existing deterministic source resolution.',
                      'source_boundary': 'post-mineru-short/v1',
                      'pause_digest': handoff['pause']['evidence_digest'],
                      'automatic_approval_activation': True, 'expected_workflow_revision': workflow['revision'],
                      'expected_organization_revision': org['registry_revision'],
                      'expected_registry_revision': registry['registry_revision'],
                      'resource_ids': sorted(pin['resource_id'] for pin in handoff['pins'])}
        resolution['input_digest'] = digest(resolution)
        preview = repo.resolve_sources(resolution, dry_run=True)
        runner.save(root / 'source-qualification-preview.json', preview)
        require(preview['resolved'], 'source resolution found no exact declared source identity')
        resolution['preview_digest'] = preview['preview_digest']
        resolution['input_digest'] = digest({k: v for k, v in resolution.items() if k != 'input_digest'})
        runner.save(path, resolution)
    result = repo.resolve_sources(resolution)
    runner.save(result_path, result)
    return result


def accept(vault, operation, delivery, provider, host_state):
    service = FileVaultFinalizeService(vault)
    release_id = delivery['release_id']
    require(service.status()['current_release_id'] == release_id, 'Final Acceptance release changed')
    verified = service.validate(release_id)
    path = service._manifest_path(release_id)
    manifest = _load_json(path)
    validate_retrieval_snapshot(service, manifest)
    included = {s['page_id'] for s in manifest['subjects']}
    revisions = {(s['page_id'], s['current_revision_id']) for s in manifest['subjects']}
    round_pages = {(s['page_id'], s['revision_id']) for s in delivery['candidates'] if s['published']}
    require(round_pages and round_pages <= revisions, 'the exact round revisions must be in the release')
    expected = {s['id'] for s in manifest['index_eligibility'] if s['kind'] == 'knowledge_page' and s['eligible']}
    require(included == expected and provider['release_hash'] == runner.sha(path), 'release and index scope differ')
    generation = host_state / provider['vault_id'] / 'generations' / provider['index_generation']
    bm25 = _load_json(generation / 'bm25.json')
    with closing(sqlite3.connect((generation / 'chroma/chroma.sqlite3').as_uri() + '?mode=ro', uri=True)) as conn:
        chroma = {r[0] for r in conn.execute("SELECT string_value FROM embedding_metadata WHERE key='page_id'") if r[0]}
        indexed = {r[0] for r in conn.execute('SELECT embedding_id FROM embeddings')}
    require(chroma == included == {r['page_id'] for r in bm25['meta']}
            and indexed == {r['id'] for r in bm25['meta']} and len(indexed) == provider['document_count'],
            'Chroma/BM25 do not match the approved exact release')
    kb, kc, _ = runner.runtime()
    with closing(kc.connect(board=operation['board'])) as conn:
        tasks = kb.list_tasks(conn)
        require(not any(t.claim_lock or t.status in ('ready', 'running', 'todo', 'triage') for t in tasks),
                'Final Acceptance requires the native short board to drain')
    require(not runner.workflow_processes(operation['origin']['workflow_id'], operation['board']),
            'Final Acceptance requires no active native consumers')
    return {'release': verified, 'provider_generation': provider['index_generation'],
            'chroma_pages': len(chroma), 'bm25_pages': len(bm25['meta']), 'provider_documents': len(indexed),
            'native_board': operation['board'], 'native_tasks': len(tasks), 'round_published_pages': len(round_pages),
            'state': 'partial',
            'coverage_complete': False, 'final_acceptance': True}


def run_controlled(args, request, root, vault):
    """Operate one console run using its original inputs and checkpoint evidence."""
    require(not os.environ.get('HERMES_KANBAN_TASK') and not os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'),
            'console controls require a trusted operator')
    short = root / 'native'
    marker = root / 'pipeline-start.json'
    operation_id = request['operation_id']
    request_sha = runner.sha(args.request)
    require(request['test_mode'] == BOOTSTRAP_SHORT and 'test' in request['security_domain'].split('-'),
            'console controls require an independent bootstrap short test')
    require(not (root / 'final-acceptance.json').exists() and not (short / 'stop.json').exists(),
            'a completed or explicitly stopped operation cannot be attached or continued')
    control = OperatorControl(root, operation_id, args.operator_attempt, args.pause_before,
                              args.continue_operation, args.attach_source)
    checkpoint = control.checkpoint
    try:
        launch = _load_json(marker) if marker.is_file() else None
        config = args.provider_config.resolve(strict=True)
        settings = {'provider_config': str(config), 'provider_sha256': runner.sha(config),
                    'provider_state': str(args.provider_state.resolve())}
        if launch:
            require((args.attach_source or args.continue_operation) and launch['operation_id'] == operation_id
                    and launch['request_sha256'] == request_sha and launch['path'] == 'bootstrap-short'
                    and launch.get('operator_settings', settings) == settings and not launch.get('reused_handoff'),
                    'attach or continue requires the exact original console request and Provider configuration')
        else:
            require(not (args.attach_source or args.continue_operation), 'original launcher identity is missing')
            launch = {'started_at': time.time(), 'operation_id': operation_id, 'path': 'bootstrap-short',
                      'request_sha256': request_sha, 'operator_settings': settings}
            runner.save(marker, launch)
        ledger = _vault_path(vault, f'_system/ledgers/ingest-workflows/ingest-{operation_id}.json')
        if not args.attach_source and checkpoint('bootstrap', {'request_sha256': request_sha}):
            return PAUSED
        if args.attach_source:
            require(ledger.is_file(), 'attachment requires the existing native source ledger')
        elif not ledger.is_file():
            from ingest_kanban import KanbanCLI

            require(KanbanCLI().dispatcher_available(),
                    'native dispatcher required before starting source preparation')
        handoff = prepare(vault, request, root, args.provisioner_skill.resolve(), args.bootstrap_skill.resolve(),
                          attach=args.attach_source or (args.continue_operation and ledger.is_file()),
                          checkpoint=checkpoint)
        if handoff is None:
            return PAUSED
        qualification = qualify_sources(vault, root, handoff, request)
        require(qualification['ok'] and qualification['resolved'] == len(handoff['pins']),
                'exact source qualification must match the verified handoff')
        prior_node = control.resume['node'] if control.resume else None
        native_checkpoint = control.resume['evidence'].get('checkpoint_id') if control.resume else None
        if prior_node not in ('formal-build', 'release', 'acceptance'):
            result = runner.start(short, vault, handoff['workflow_id'], handoff['pins'], operation_id,
                                  checkpoint=checkpoint, continue_checkpoint=native_checkpoint)
            if result['state'] == 'paused':
                return PAUSED
        result = _load_json(short / 'result.json')
        sealed = runner.settled_checkpoint(short)
        plan = sorted(short.glob('stage-*-offline_build.json'))[-1]
        partition = delivery_partition(_load_json(plan))
        require(partition['included'], 'no pages with verified complete fragment coverage')
        authorization = {'contract': 'short-formal-authorization/v1', 'writing_entry': 'initial',
                         'checkpoint': sealed, 'actor': request['actor'], 'pages': partition['included'],
                         'base_release': None, 'publish': True, 'policy': POLICY}
        auth_path = root / 'formal-authorization.json'
        if auth_path.is_file():
            require(_load_json(auth_path) == authorization, 'formal authorization changed')
        else:
            runner.save(auth_path, authorization)
        operation = _load_json(short / 'operation.json')
        if prior_node in ('formal-build', 'release', 'acceptance'):
            final_plan(short, operation, authorization, runner)
        if checkpoint('formal-build', {**sealed, 'authorization_sha256': runner.sha(auth_path)}):
            return PAUSED
        delivery = execute(short, 'formal-fifth-release', authorization, False, runner, before_delivery=checkpoint)
        if delivery is None:
            return PAUSED
        require(delivery['release_id'], 'formal eligibility produced no releasable pages')
        provider_path = root / 'provider-result.json'
        if prior_node == 'acceptance':
            saved = _load_json(provider_path)
            provider = saved['index']
            require(saved['returncode'] == 0 and provider['status'] == 'ready'
                    and provider['release_id'] == delivery['release_id'], 'Provider checkpoint changed')
        else:
            provider = synchronize(vault, root, delivery['release_id'], config)
        if checkpoint('acceptance', {'release_id': delivery['release_id'], 'provider_sha256': runner.sha(provider_path),
                                     'authorization_sha256': runner.sha(auth_path)}):
            return PAUSED
        final = accept(vault, operation, delivery, provider, args.provider_state.resolve())
        final.update(source_handoff=handoff, source_qualification=qualification, short_result=result, delivery=delivery,
                     wall_seconds=time.time() - launch['started_at'],
                     whole_run_continuous_automation=not (args.attach_source or args.continue_operation))
        runner.save(root / 'final-acceptance.json', final)
        control.finish('completed')
        print(json.dumps(final['release'], ensure_ascii=False), flush=True)
    except SourceWaitDetached as error:
        control.finish('detached', error=str(error))
        print(str(error), flush=True)
        return DETACHED
    except Exception as error:
        control.finish('failed', error=str(error))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--provisioner-skill', type=Path, required=True)
    parser.add_argument('--bootstrap-skill', type=Path, required=True)
    parser.add_argument('--provider-config', type=Path, required=True)
    parser.add_argument('--provider-state', type=Path, required=True)
    parser.add_argument('--resume-delivery', action='store_true')
    parser.add_argument('--content-isolation', type=Path)
    parser.add_argument('--from-node', choices=('mapping', 'coordination', 'writing', 'formal-build'))
    parser.add_argument('--reentry-id')
    parser.add_argument('--prior-entry')
    parser.add_argument('--fragment-repair', type=Path)
    parser.add_argument('--recover-held-content', action='store_true',
                        help='Classify held native content, allow its one remaining call, and deliver complete sections')
    parser.add_argument('--replay-writing', action='store_true',
                        help='Recover an unpublished writing checkpoint from exact returned attempts with zero model calls')
    parser.add_argument('--split-atomic-fragment')
    parser.add_argument('--mapping-recovery', type=Path)
    parser.add_argument('--source-registry-revision', type=int)
    parser.add_argument('--repeat-from', type=Path, help='Reuse the exact source handoff in a previous round directory')
    parser.add_argument('--round-id', help='Stable board and page namespace for a new same-Vault round')
    parser.add_argument('--mapping-from', type=Path, help='Start a matched mapping comparison using a settled short pipeline')
    parser.add_argument('--mapping-fence-from', type=Path, action='append', default=[])
    parser.add_argument('--transport-profile', choices=('concentrated', 'paced'))
    control_group = parser.add_mutually_exclusive_group()
    control_group.add_argument('--attach-source', action='store_true')
    control_group.add_argument('--continue-operation', action='store_true')
    parser.add_argument('--operator-attempt')
    parser.add_argument('--pause-before', choices=NODES)
    args = parser.parse_args()
    request = _load_json(args.request.resolve(strict=True))
    root = args.output.resolve()
    vault = Path(request['vault_path']).resolve()
    require(not args.replay_writing or (args.from_node == 'writing' and not any((
        args.fragment_repair, args.split_atomic_fragment, args.prior_entry, args.mapping_recovery,
        args.resume_delivery, args.content_isolation))), 'writing replay requires an isolated writing reentry')
    require(not root.is_relative_to(vault), 'test evidence must remain outside the Vault')
    require(not args.recover_held_content or (args.from_node == 'writing'
            and not any((args.fragment_repair, args.replay_writing, args.split_atomic_fragment))),
            'content recovery requires an independent writing reentry')
    require(bool(args.repeat_from) == bool(args.round_id)
            and (not args.round_id or (re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', args.round_id)
                                      and args.round_id != request['operation_id']
                                      and not args.from_node and not args.resume_delivery)),
            'a fresh same-Vault round requires its own ID and previous source handoff')
    require(bool(args.mapping_from) == bool(args.transport_profile) and (not args.mapping_from or args.repeat_from)
            and (not args.mapping_fence_from or args.mapping_from),
            'a matched mapping comparison requires a same-Vault round and a transport profile')
    root.mkdir(parents=True, exist_ok=True)
    with (runner.exclusive_lock(vault.parent / ('.' + vault.name + '-short-pipeline.lock'), timeout=0, marker=False),
          runner.exclusive_lock(root / 'pipeline.lock', timeout=0, marker=False)):
        if args.operator_attempt:
            require(not any((args.from_node, args.resume_delivery, args.repeat_from, args.content_isolation,
                             args.reentry_id, args.prior_entry, args.fragment_repair, args.recover_held_content,
                             args.replay_writing, args.split_atomic_fragment, args.mapping_recovery,
                             args.source_registry_revision is not None)),
                    'console controls cannot be combined with an existing recovery or comparison entry')
            return run_controlled(args, request, root, vault)
        require(not (args.attach_source or args.continue_operation or args.pause_before), 'missing operator attempt ID')
        operator_path = root / 'operator-state.json'
        require(not operator_path.is_file() or _load_json(operator_path)['state'] != 'paused',
                'an explicit operator pause must be continued through its checkpoint')
        short = root / 'native'
        marker = root / 'pipeline-start.json'
        provider = None
        if args.from_node == 'formal-build':
            require(args.reentry_id and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', args.reentry_id)
                    and not any((args.resume_delivery, args.content_isolation, args.prior_entry,
                                 args.fragment_repair, args.split_atomic_fragment, args.mapping_recovery,
                                 args.source_registry_revision is not None)),
                    'formal replay requires its own stable entry and the original writing checkpoint')
            start = time.time()
            prior = _load_json(marker)
            operation = _load_json(short / 'operation.json')
            original = _load_json(root / 'final-acceptance.json')
            handoff = _load_json(root / 'source-handoff.json')
            qualification = _load_json(root / 'source-qualification.json')
            result = _load_json(short / 'result.json')
            authorization = _load_json(root / 'formal-authorization.json')
            committed = original['delivery']
            from hermes_source_units.runtime_policy import POLICY_REF
            policy = _load_json(_vault_path(vault, POLICY_REF))
            require(prior['request_sha256'] == runner.sha(args.request) and Path(operation['vault']) == vault
                    and 'test' in request['security_domain'].split('-') and policy == request['retrieval_policy']
                    and policy['sync_enabled'] is True and policy['query_enabled'] is False
                    and original['final_acceptance'] and original['short_result'] == result
                    and original['source_handoff'] == handoff and qualification == original['source_qualification']
                    and committed['release_id'] == release_identity(committed['run_id'], authorization),
                    'formal replay requires the exact accepted isolated short delivery')
            entry = 'formal-replay-' + args.reentry_id
            directory = short / 'reentries' / entry
            print('Replaying the settled writing checkpoint through a new native formal Build', flush=True)
            delivery = execute(short, entry, authorization, False, runner, replay_build=committed['run_id'])
            require(delivery == committed, 'formal replay changed the accepted page revisions or exclusions')
        elif args.from_node:
            entry_pattern = r'[a-z0-9][a-z0-9-]{0,31}'
            writing = args.from_node == 'writing'
            coordination = args.from_node == 'coordination'
            recovery_args = (args.fragment_repair, args.split_atomic_fragment, args.replay_writing,
                             args.recover_held_content, args.mapping_recovery, args.source_registry_revision)
            require(args.reentry_id and re.fullmatch(entry_pattern, args.reentry_id)
                    and not args.resume_delivery and not args.content_isolation
                    and (not any(recovery_args) if coordination else
                         bool(args.fragment_repair or args.split_atomic_fragment or args.replay_writing or args.recover_held_content) and not args.mapping_recovery if writing else
                         bool(args.mapping_recovery) and not args.fragment_repair and not args.split_atomic_fragment)
                    and (not args.prior_entry or (writing and re.fullmatch(entry_pattern, args.prior_entry))),
                    'supplement requires a stable reentry and its node-specific manifest')
            start = time.time()
            prior = _load_json(marker)
            require(prior['request_sha256'] == runner.sha(args.request), 'supplement request identity changed')
            handoff = _load_json(root / 'source-handoff.json')
            qualification = _load_json(root / 'source-qualification.json')
            prior_output = short / 'reentries' / args.prior_entry if args.prior_entry else root
            service = FileVaultFinalizeService(vault)
            original = None
            if args.replay_writing or coordination:
                require(not (root / 'final-acceptance.json').exists()
                        and not service.status()['current_release_id'] and 'test' in request['security_domain'].split('-'),
                        'initial recovery requires the unpublished isolated Vault')
                if coordination:
                    interrupted = _load_json(short / 'result.json')
                    require(interrupted['last_stage'] == 'coordination_held'
                            and not interrupted['native_active_or_ready'],
                            'coordination recovery requires a stopped initial coordination hold')
                base_release = None
            else:
                original = _load_json(prior_output / 'final-acceptance.json')
                base_release = original['delivery']['release_id']
                require(service.status()['current_release_id'] == base_release and service.validate(base_release)['ok'],
                        'prior release must remain the exact supplement base')
            recovery = _load_json(args.mapping_recovery) if args.mapping_recovery else None
            if recovery:
                require(recovery['prior_build'] == original['delivery']['run_id'], 'supplement prior Build changed')
            print(f"Recovering the affected {args.from_node} work", flush=True)
            result = runner.start(short, vault, handoff['workflow_id'], handoff['pins'],
                                  prior.get('round_id', request['operation_id']),
                                  node=args.from_node, entry=args.reentry_id, mapping_recovery=recovery,
                                  fragment_repair=_load_json(args.fragment_repair) if args.fragment_repair else None,
                                  split_output=args.split_atomic_fragment,
                                  registry_revision=args.source_registry_revision, replay_only=args.replay_writing,
                                  content_recovery=args.recover_held_content)
            directory = short / 'reentries' / args.reentry_id
            require(result['last_stage'] == 'offline_build', 'supplement did not reach a settled writing checkpoint')
            plan_path = sorted(directory.glob('stage-*-offline_build.json'))[-1]
            partition = delivery_partition(_load_json(plan_path))
            published = {p['short_id'] for p in original['delivery']['candidates'] if p['published']} if writing and original else set()
            selected = [pid for pid in partition['included'] if pid not in published]
            authorization = {'contract': 'short-formal-authorization/v1', 'writing_entry': args.reentry_id,
                             'checkpoint': {'control_sha256': runner.sha(directory / 'control.json'),
                                            'result_sha256': runner.sha(directory / 'result.json'),
                                            'plan_sha256': runner.sha(plan_path)},
                             'actor': request['actor'], 'pages': selected, 'base_release': base_release,
                             'publish': True, 'policy': POLICY}
            if (directory / 'content-recovery.json').is_file():
                authorization['checkpoint']['content_recovery_sha256'] = runner.sha(directory / 'content-recovery.json')
            runner.save(directory / 'formal-authorization.json', authorization)
            if selected:
                delivery = execute(short, 'formal-' + args.reentry_id, authorization, False, runner)
            else:
                require(writing and original, 'recovery found no complete affected pages')
                final_plan(short, _load_json(short / 'operation.json'), authorization, runner)
                delivery = original['delivery']
                provider_result = _load_json(prior_output / 'provider-result.json')
                provider = provider_result['index']
                runner.save(directory / 'provider-result.json', {**provider_result, 'reused_from': str(prior_output)})
                runner.save(directory / 'delivery-reuse.json', {
                    'release_id': base_release, 'provider_generation': provider['index_generation'],
                    'writing_entry': args.reentry_id, 'no_new_complete_page': True,
                    'excluded': partition['excluded'], 'retained_short_pages': sorted(published)})
        elif args.resume_delivery:
            prior = _load_json(marker)
            require(prior['request_sha256'] == runner.sha(args.request)
                    and not (root / 'final-acceptance.json').exists(), 'delivery reentry identity changed')
            start = prior['started_at']
            handoff = _load_json(root / 'source-handoff.json')
            result = _load_json(short / 'result.json')
            authorization = _load_json(root / 'formal-authorization.json')
            qualification = qualify_sources(vault, root, handoff, request)
            first = short / 'reentries/formal-fifth-release/result.json'
            if first.is_file():
                committed = _load_json(first)
                if args.content_isolation:
                    service = FileVaultFinalizeService(vault)
                    run = _load_json(service.build._run_path(committed['run_id']))
                    record_content_isolation(service, run, _load_json(args.content_isolation), authorization)
                print('Source qualification settled; reusing the existing Build for release', flush=True)
                delivery = publish(vault, committed['run_id'], authorization)
            else:
                require(not args.content_isolation, 'content isolation requires a completed Build')
                print('Reusing the settled writing checkpoint for one formal Build recovery', flush=True)
                delivery = execute(short, 'formal-build-recovery', authorization, False, runner)
        else:
            require(not marker.exists() and not args.content_isolation, 'stable run already started or unsupported early isolation')
            start = time.time()
            round_id = args.round_id or request['operation_id']
            launch = {'started_at': start, 'operation_id': request['operation_id'],
                      'path': 'bootstrap-short' if request['test_mode'] == BOOTSTRAP_SHORT else 'post-mineru-short',
                      'request_sha256': runner.sha(args.request)}
            base_release = None
            if args.repeat_from:
                previous = args.repeat_from.resolve(strict=True)
                require(previous != root and not previous.is_relative_to(vault), 'repeat requires independent evidence')
                handoff, qualification = repeat_handoff(vault, args.request, previous)
                service = FileVaultFinalizeService(vault)
                base_release = service.status()['current_release_id']
                require(not base_release or service.validate(base_release)['ok'], 'previous release is not verified')
                launch.update(round_id=round_id, reused_handoff=str(previous), base_release=base_release)
                runner.save(marker, launch)
                runner.save(root / 'source-handoff.json', handoff)
                runner.save(root / 'source-qualification.json', qualification)
            else:
                from ingest_kanban import KanbanCLI

                require(KanbanCLI().dispatcher_available(),
                        'native Hermes Gateway dispatcher must be available before source provisioning')
                runner.save(marker, launch)
                print('Starting independent native source preparation', flush=True)
                handoff = prepare(vault, request, root, args.provisioner_skill.resolve(), args.bootstrap_skill.resolve())
                qualification = qualify_sources(vault, root, handoff, request)
            mapping_from = args.mapping_from.resolve(strict=True) / 'native' if args.mapping_from else None
            fences = tuple(p.resolve(strict=True) / 'native' for p in args.mapping_fence_from)
            if mapping_from:
                require(all(not p.is_relative_to(vault) and p != short for p in (mapping_from, *fences)),
                        'mapping evidence must be independent')
            print(f"Source handoff verified at revision {handoff['revision']}; starting short stages", flush=True)
            result = runner.start(short, vault, handoff['workflow_id'], handoff['pins'], round_id,
                                  separate_page_identities=bool(args.repeat_from), mapping_from=mapping_from,
                                  transport_profile=args.transport_profile or 'shared', mapping_fences=fences)
            require(result['last_stage'] == 'offline_build', 'short stages did not produce a settled Build input')
            plan_path = sorted(short.glob('stage-*-offline_build.json'))[-1]
            partition = delivery_partition(_load_json(plan_path))
            require(partition['included'], 'no pages with verified complete fragment coverage')
            authorization = {'contract': 'short-formal-authorization/v1', 'writing_entry': 'initial',
                             'checkpoint': {'control_sha256': runner.sha(short / 'operation.json'),
                                            'result_sha256': runner.sha(short / 'result.json'),
                                            'plan_sha256': runner.sha(plan_path)},
                             'actor': request['actor'], 'pages': partition['included'], 'base_release': base_release,
                             'publish': True, 'policy': POLICY}
            if (short / 'content-recovery.json').is_file():
                authorization['checkpoint']['content_recovery_sha256'] = runner.sha(short / 'content-recovery.json')
            runner.save(root / 'formal-authorization.json', authorization)
            print(f"{len(partition['included'])} complete pages; executing formal Build and release", flush=True)
            delivery = execute(short, 'formal-fifth-release', authorization, False, runner)
        require(delivery['release_id'], 'formal eligibility produced no releasable pages')
        output = directory if args.from_node else root
        if provider is None:
            print(f"Release {delivery['release_id']} applied; synchronizing Provider", flush=True)
            provider = synchronize(vault, output, delivery['release_id'], args.provider_config.resolve())
        else:
            print(f"Verifying unchanged release {delivery['release_id']} and its existing Provider index", flush=True)
        final = accept(vault, _load_json(short / 'operation.json'), delivery, provider, args.provider_state.resolve())
        final.update(source_handoff=handoff, short_result=result, delivery=delivery, wall_seconds=time.time() - start,
                     source_qualification=qualification,
                     whole_run_continuous_automation=not (args.resume_delivery or args.replay_writing)
                     and args.from_node not in ('coordination', 'formal-build'))
        if args.from_node == 'formal-build':
            final.update(replayed_from='formal-build', supplement_continuous_automation=True)
        runner.save(output / 'final-acceptance.json', final)
        print(json.dumps(final['release'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SourceWaitDetached as error:
        print(str(error), flush=True)
        sys.exit(DETACHED)
