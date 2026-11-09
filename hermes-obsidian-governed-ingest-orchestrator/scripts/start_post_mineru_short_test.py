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
from hermes_source_units.page_governance import POLICY, validate_retrieval_snapshot
from hermes_source_units.short_build import record_content_isolation
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from short_evidence_pass import require
from short_formal_build import delivery as publish, execute, final_plan
from short_fragment_disposition import delivery_partition


def prepare(vault, request, root, provisioner, bootstrap):
    wid = 'ingest-' + request['operation_id']
    require(request['test_mode'] == 'post-mineru-test/v1' and request['pause_after'] == ['exact_plan']
            and request['retrieval_policy']['sync_enabled'] and not request['retrieval_policy']['query_enabled'],
            'independent short test requires an isolated exact-plan source boundary and scoped Provider')
    path = root / 'provisioning-request.json'
    runner.save(path, request)
    receipt = root / 'provisioning-receipt.json'
    start = time.time()
    cmd = [sys.executable, '-B', str(provisioner / 'scripts/start_post_mineru_test.py'),
           '--request', str(path), '--record', str(receipt), '--bootstrap-skill', str(bootstrap)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=180)
    require(result.returncode == 0, 'native source provisioning entry failed: ' + result.stderr[-2500:])
    ledger = _vault_path(vault, f'_system/ledgers/ingest-workflows/{wid}.json')
    deadline = time.monotonic() + 1200
    kb, kc, _ = runner.runtime()
    while True:
        workflow = _load_json(ledger)
        require(workflow['state'] not in ('failed', 'cancelled', 'partial', 'completed'),
                'provisioning ended before the exact-plan source handoff')
        board = workflow['kanban']['board_id']
        if workflow.get('pause_control', {}).get('boundary') == 'exact_plan':
            with closing(kc.connect(board=board)) as conn:
                active = any(t.status == 'running' or t.claim_lock for t in kb.list_tasks(conn))
            if not active and not runner.workflow_processes(wid, board):
                break
        require(time.monotonic() < deadline, 'native source handoff timed out')
        time.sleep(2)
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
    runner.source_boundary(vault, wid, pins)
    handoff = {'workflow_id': wid, 'revision': workflow['revision'], 'pause': workflow['pause_control'],
               'import_receipt': startup['test_bundle_import'], 'pins': pins, 'seconds': time.time() - start,
               'mineru_calls': imported['mineru_calls'], 'imported_files': sum(len(s['origin_files']) for s in imported['sources'])}
    runner.save(root / 'source-handoff.json', handoff)
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
                      'reason': 'Authorized independent post-MinerU short test; existing deterministic source resolution.',
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
    parser.add_argument('--from-node', choices=('mapping', 'writing'))
    parser.add_argument('--reentry-id')
    parser.add_argument('--prior-entry')
    parser.add_argument('--fragment-repair', type=Path)
    parser.add_argument('--split-atomic-fragment')
    parser.add_argument('--mapping-recovery', type=Path)
    parser.add_argument('--source-registry-revision', type=int)
    parser.add_argument('--repeat-from', type=Path, help='Reuse the exact source handoff in a previous round directory')
    parser.add_argument('--round-id', help='Stable board and page namespace for a new same-Vault round')
    args = parser.parse_args()
    request = _load_json(args.request.resolve(strict=True))
    root = args.output.resolve()
    vault = Path(request['vault_path']).resolve()
    require(not root.is_relative_to(vault), 'test evidence must remain outside the Vault')
    require(bool(args.repeat_from) == bool(args.round_id)
            and (not args.round_id or (re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', args.round_id)
                                      and args.round_id != request['operation_id']
                                      and not args.from_node and not args.resume_delivery)),
            'a fresh same-Vault round requires its own ID and previous source handoff')
    root.mkdir(parents=True, exist_ok=True)
    with (runner.exclusive_lock(vault.parent / ('.' + vault.name + '-short-pipeline.lock'), timeout=0, marker=False),
          runner.exclusive_lock(root / 'pipeline.lock', timeout=0, marker=False)):
        short = root / 'native'
        marker = root / 'pipeline-start.json'
        provider = None
        if args.from_node:
            entry_pattern = r'[a-z0-9][a-z0-9-]{0,31}'
            writing = args.from_node == 'writing'
            require(args.reentry_id and re.fullmatch(entry_pattern, args.reentry_id)
                    and not args.resume_delivery and not args.content_isolation
                    and (bool(args.fragment_repair or args.split_atomic_fragment) and not args.mapping_recovery if writing else
                         bool(args.mapping_recovery) and not args.fragment_repair and not args.split_atomic_fragment)
                    and (not args.prior_entry or (writing and re.fullmatch(entry_pattern, args.prior_entry))),
                    'supplement requires a stable reentry and its node-specific manifest')
            start = time.time()
            prior = _load_json(marker)
            require(prior['request_sha256'] == runner.sha(args.request), 'supplement request identity changed')
            handoff = _load_json(root / 'source-handoff.json')
            qualification = _load_json(root / 'source-qualification.json')
            prior_output = short / 'reentries' / args.prior_entry if args.prior_entry else root
            original = _load_json(prior_output / 'final-acceptance.json')
            service = FileVaultFinalizeService(vault)
            base_release = original['delivery']['release_id']
            require(service.status()['current_release_id'] == base_release and service.validate(base_release)['ok'],
                    'prior release must remain the exact supplement base')
            recovery = _load_json(args.mapping_recovery) if not writing else None
            if recovery:
                require(recovery['prior_build'] == original['delivery']['run_id'], 'supplement prior Build changed')
            print(f"Recovering the affected {args.from_node} work", flush=True)
            result = runner.start(short, vault, handoff['workflow_id'], handoff['pins'],
                                  prior.get('round_id', request['operation_id']),
                                  node=args.from_node, entry=args.reentry_id, mapping_recovery=recovery,
                                  fragment_repair=_load_json(args.fragment_repair) if args.fragment_repair else None,
                                  split_output=args.split_atomic_fragment,
                                  registry_revision=args.source_registry_revision)
            directory = short / 'reentries' / args.reentry_id
            require(result['last_stage'] == 'offline_build', 'supplement did not reach a settled writing checkpoint')
            plan_path = sorted(directory.glob('stage-*-offline_build.json'))[-1]
            partition = delivery_partition(_load_json(plan_path))
            published = {p['short_id'] for p in original['delivery']['candidates'] if p['published']} if writing else set()
            selected = [pid for pid in partition['included'] if pid not in published]
            authorization = {'contract': 'short-formal-authorization/v1', 'writing_entry': args.reentry_id,
                             'checkpoint': {'control_sha256': runner.sha(directory / 'control.json'),
                                            'result_sha256': runner.sha(directory / 'result.json'),
                                            'plan_sha256': runner.sha(plan_path)},
                             'actor': request['actor'], 'pages': selected, 'base_release': base_release,
                             'publish': True, 'policy': POLICY}
            runner.save(directory / 'formal-authorization.json', authorization)
            if selected:
                delivery = execute(short, 'formal-' + args.reentry_id, authorization, False, runner)
            else:
                require(writing, 'mapping supplement found no complete affected pages')
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
            first = _load_json(short / 'reentries/formal-fifth-release/result.json')
            service = FileVaultFinalizeService(vault)
            run = _load_json(service.build._run_path(first['run_id']))
            if args.content_isolation:
                record_content_isolation(service, run, _load_json(args.content_isolation), authorization)
            qualification = qualify_sources(vault, root, handoff, request)
            print('Source qualification settled; reusing the existing Build for release', flush=True)
            delivery = publish(vault, first['run_id'], authorization)
        else:
            require(not marker.exists() and not args.content_isolation, 'stable run already started or unsupported early isolation')
            start = time.time()
            round_id = args.round_id or request['operation_id']
            launch = {'started_at': start, 'operation_id': request['operation_id'],
                      'path': 'post-mineru-short', 'request_sha256': runner.sha(args.request)}
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
            print(f"Source handoff verified at revision {handoff['revision']}; starting short stages", flush=True)
            result = runner.start(short, vault, handoff['workflow_id'], handoff['pins'], round_id,
                                  separate_page_identities=bool(args.repeat_from))
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
                     source_qualification=qualification, whole_run_continuous_automation=not args.resume_delivery)
        runner.save(output / 'final-acceptance.json', final)
        print(json.dumps(final['release'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
