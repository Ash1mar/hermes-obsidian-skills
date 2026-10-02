#!/usr/bin/env python3
"""Release gate: actual ready sources + Gateway/model/workers/watcher + eight Pass slices.

Run in the Hermes Linux runtime. Clones inputs into a temporary sibling Vault on
the source Vault's filesystem; all host state and credentials stay in an isolated
Linux Hermes home. Never completes a card or writes semantic Pass evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_preflight_evidence(home, snapshots):
    """Read native terminal transcripts; never run or manufacture a Pass."""
    import re
    sessions = []
    for snapshot in snapshots:
        ids = {snapshot['task']['session_id']} if snapshot['task'].get('session_id') else set()
        if not ids:
            raise RuntimeError('native Pass snapshot has no session identity for preflight audit')
        sessions.append((snapshot['task']['id'], ids))
    database = home/'state.db'
    result = []
    with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as connection:
        for task_id, ids in sessions:
            calls, successes = 0, 0
            for session_id in sorted(ids):
                for role, content, tool_calls in connection.execute(
                        'SELECT role, content, tool_calls FROM messages WHERE session_id=?', (session_id,)):
                    if tool_calls and 'batch-pass' in tool_calls and '--validate-only' in tool_calls and '--worker-binding' in tool_calls:
                        calls += 1
                    if role == 'tool' and content:
                        # Terminal responses may encode stdout as a nested JSON string.
                        try:
                            parsed = json.loads(content)
                        except ValueError:
                            parsed = content
                        texts = [content]
                        if isinstance(parsed, dict):
                            texts.extend(v for v in parsed.values() if isinstance(v, str))
                        if any(re.search(r'"validated"\s*:\s*true', text)
                               and all('"' + key + '"' in text for key in ('batch', 'results', 'failures'))
                               for text in texts):
                            successes += 1
            if not calls or not successes:
                raise RuntimeError(f'native Pass {task_id} has no successful bound preflight transcript')
            result.append({'task_id': task_id, 'session_ids': sorted(ids),
                           'preflight_tool_calls': calls, 'successful_result_messages': successes})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-vault', required=True)
    parser.add_argument('--workflow-id', required=True)
    parser.add_argument('--hermes-home', default='/root/.hermes')
    parser.add_argument('--output', required=True)
    parser.add_argument('--timeout', type=int, default=7200)
    parser.add_argument('--measurements-from', help='Previous isolated gate Vault; checkpoints are freshly verified')
    parser.add_argument('--verify-pauses', action='store_true',
                        help='Verify exact-plan pause, explicit continuation and eight-slice pause')
    parser.add_argument('--verify-preflight', action='store_true',
                        help='Require successful bound draft preflight in all eight native worker sessions')
    args = parser.parse_args()
    if os.name != 'posix':
        raise RuntimeError('this gate must use the deployed Hermes Linux runtime')
    repo = Path(__file__).resolve().parents[2]
    original = Path(args.source_vault).resolve()
    report = Path(args.output).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    original_ledger = original/f'_system/ledgers/ingest-workflows/{args.workflow_id}.json'
    before = original_ledger.read_bytes()
    old = json.loads(before)
    raw = {p:sha(original/p) for p in old['scope']['source_paths']}
    state = Path(tempfile.mkdtemp(prefix='hermes-canary-state-'))
    home = state/'home'
    home.mkdir(mode=0o700)
    # Keep the isolated Vault on the same filesystem as the source Vault.
    scratch_parent = original.parent/'tmp'
    scratch_parent.mkdir(parents=True, exist_ok=True)
    if scratch_parent.stat().st_dev != original.stat().st_dev:
        raise RuntimeError('isolated Vault must use the source Vault filesystem')
    sandbox = Path(tempfile.mkdtemp(prefix='governed-canary-', dir=scratch_parent))
    vault = sandbox/'vault'
    (vault/'_system').mkdir(parents=True)
    evidence = {'passed':False, 'source_vault':str(original), 'vault':str(vault),
        'runtime':str(state), 'workflow_id':args.workflow_id,
        'started_at':time.time(), 'samples':[], 'root_isolation':False,
        'manual_card_completion':False, 'synthetic_passes':False}
    gateway = None
    watcher_pid = None
    def save():
        report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
    save()
    try:
        skills = ['hermes-obsidian-'+s for s in ('controlled-ingest','controlled-query',
            'governed-ingest-orchestrator','knowledge-finalize','vault-bootstrap','vault-lint')]
        for name in skills:
            shutil.copytree(repo/name, home/'skills/domain'/name,
                            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            for script in (home/'skills/domain'/name/'scripts').glob('*.py'):
                script.chmod(0o755)
        def candidate_hashes(base):
            return {f'{name}/{p.relative_to(base/name).as_posix()}':hashlib.sha256(
                        p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
                    for name in skills for p in (base/name).rglob('*')
                    if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
        fixed_hashes = candidate_hashes(home/'skills/domain')
        assert candidate_hashes(repo)==fixed_hashes
        evidence['candidate_files'] = fixed_hashes
        evidence['timeout_seconds'] = args.timeout
        import yaml
        config = yaml.safe_load((Path(args.hermes_home)/'config.yaml').read_text())
        config.setdefault('gateway', {})['multiplex_profiles'] = False
        config.setdefault('kanban', {})['dispatch_in_gateway'] = True
        # Keep the deployed concurrency/model/interval. Only runtime paths change.
        config.setdefault('terminal', {})['cwd'] = str(sandbox)
        (home/'config.yaml').write_text(yaml.safe_dump(config))
        shutil.copy2(Path(args.hermes_home)/'auth.json', home/'auth.json')
        (home/'auth.json').chmod(0o600)
        os.environ['HERMES_HOME'] = str(home)
        os.environ['HERMES_KANBAN_HOME'] = str(home)
        os.environ['HERMES_GATEWAY_LOCK_DIR'] = str(state/'gateway-locks')
        for key in ('HERMES_KANBAN_TASK','HERMES_KANBAN_BOARD','HERMES_DELEGATED_CHILD_CONTEXT'):
            if os.environ.get(key):
                raise RuntimeError('gate requires a trusted non-worker process')
        sys.path.insert(0, '/usr/local/lib/hermes-agent')
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.kanban_db import kanban_home
        from gateway.host_rendezvous import host_state_dir, read_record, ROLE_GATEWAY
        assert get_default_hermes_root()==home and get_hermes_home()==home and kanban_home()==home
        assert host_state_dir()==state/'gateway-locks' and read_record(ROLE_GATEWAY) is None
        evidence['root_isolation'] = True
        # Copy only candidate-controlled inputs, never the Provider data plane.
        shutil.copy2(original/'_system/vault.json', vault/'_system/vault.json')
        for name in ('metadata','sources'):
            shutil.copytree(original/'_system'/name, vault/'_system'/name)
        for path in old['scope']['source_paths']:
            dest = vault/path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original/path, dest)
        # Pause evidence also pins historical preparation reports and Bundle
        # manifests. These are authoritative inputs, unlike Provider state.
        for outcome in old.get('source_outcomes', []):
            for ref in outcome.get('artifact_refs', []):
                source = (original/ref).resolve()
                source.relative_to(original)
                dest = vault/ref
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                if source.name=='manifest.json' and 'converted' in source.parts:
                    shutil.copytree(source.parent, dest.parent, dirs_exist_ok=True)
        ledger = vault/f'_system/ledgers/ingest-workflows/{args.workflow_id}.json'
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_bytes(before)
        shutil.copytree(original/f'_system/ledgers/ingest-workflows/{args.workflow_id}/templates',
                        vault/f'_system/ledgers/ingest-workflows/{args.workflow_id}/templates')
        if args.measurements_from:
            previous = Path(args.measurements_from).resolve()
            if previous == original or previous.parent.parent != original.parent/'tmp':
                raise RuntimeError('checkpoint reuse must name an isolated gate Vault')
            shutil.copytree(previous/'_system/reports/exact-planning', vault/'_system/reports/exact-planning')
            evidence['checkpoint_reuse_from'] = str(previous)
        skill = home/'skills/domain/hermes-obsidian-governed-ingest-orchestrator'
        sys.path.insert(0, str(skill/'lib'))
        from hermes_source_units import FileIngestWorkflowService, mutation_digest
        from hermes_source_units.validation import fingerprint
        from ingest_kanban import IngestKanbanAdapter, KanbanCLI
        from orchestration import worker_pack, dispatch
        service = FileIngestWorkflowService(vault)
        def request(value, **extra):
            r = {'workflow_id':value['workflow_id'], 'actor':value['actor'],
                 'expected_revision':value['revision'], **extra}
            r['input_digest'] = mutation_digest(r)
            return r
        current = service.status(args.workflow_id)
        assert current['batch_id'] is None, 'this gate tests pre-batch recovery'
        if not current['cancel_requested']:
            current = service.cancel(request(current))
        current = service.repair_preparation(request(current, repair_id='release-gate',
            reason='Validate candidate deployment in isolated full-scale native workflow',
            evidence_refs=[current['template_pins'][0]['path']], templates=worker_pack(),
            reset_sources=[], execution_mode='auto_full' if args.verify_pauses else 'canary_only',
            pause_after=['exact_plan', 'canary', 'pass', 'checkpoint_1', 'build_finalize',
                         'checkpoint_2', 'release_sync'] if args.verify_pauses else []))
        assert current['source_outcomes']==old['source_outcomes']
        # No model request construction or fixture completion in the success path.
        with (state/'gateway.log').open('wb') as output:
            gateway = subprocess.Popen(['hermes','gateway','run'],stdin=subprocess.DEVNULL,
                                       stdout=output,stderr=subprocess.STDOUT)
        native = KanbanCLI()
        deadline = time.monotonic()+60
        while True:
            record = read_record(ROLE_GATEWAY)
            if record and record.pid==gateway.pid and Path(record.home)==home and native.dispatcher_available():
                break
            if gateway.poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('isolated Gateway failed to become ready')
            time.sleep(1)
        record = read_record(ROLE_GATEWAY)
        assert record and record.pid==gateway.pid and Path(record.home)==home
        evidence['isolated_host_gateway_verified'] = True
        result = dispatch(vault, 'resume', request(current))
        watcher_pid = result.get('reconciler_pid')
        evidence.update(resume=result, candidate_version=json.loads((skill/'config/orchestration.json').read_text())['template_version'],
                        gateway_pid=gateway.pid)
        save()
        print('GATE_STARTED', json.dumps({k:evidence[k] for k in ('vault','runtime','gateway_pid','candidate_version')}), flush=True)
        begun = time.monotonic()
        adapter = IngestKanbanAdapter(vault, enable_workers=True)
        def quiescent_pause(boundary):
            # The receipt precedes native card acknowledgement. Do not race a
            # still-running reconciler when testing the operator's next command.
            deadline = time.monotonic() + 180
            while watcher_pid:
                try:
                    status = Path(f'/proc/{watcher_pid}/status').read_text()
                except FileNotFoundError:
                    break
                state_line = next(line for line in status.splitlines() if line.startswith('State:'))
                if state_line.split()[1] == 'Z':
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('paused reconciler did not finish native acknowledgement')
                time.sleep(2)
            current = service.status(args.workflow_id)
            assert current['pause_control']['boundary']==boundary
            assert current['pause_control']['evidence_digest']
            return current
        last = None
        while time.monotonic()-begun < args.timeout:
            if gateway.poll() is not None:
                raise RuntimeError('isolated Gateway exited during acceptance')
            value = service.status(args.workflow_id)
            runtime = adapter.runtime_status(args.workflow_id)
            cards = runtime.get('cards', [])
            failures = [c for c in cards if c.get('state')=='execution_blocked']
            counts = {}
            for c in cards:
                label = c.get('state', c.get('kanban_status','unknown'))
                counts[label] = counts.get(label,0)+1
            summary = {'elapsed':round(time.monotonic()-begun), 'stage':value['current_stage'],
                       'batch_id':value['batch_id'], 'cards':counts}
            if summary != last:
                evidence['samples'].append(summary)
                print('GATE_PROGRESS', json.dumps(summary), flush=True)
                last = summary
                save()
            if failures:
                evidence['execution_blockers']=failures
                raise RuntimeError('native worker execution blocked; inspect report')
            if value['batch_id']:
                blocked = [s for s in service.knowledge._slices(value['batch_id'])
                           if s['slice_id'] in value['dispatch_policy']['slice_ids']
                           and s['last_error'] and s['state'] in ('blocked', 'reconcile_required', 'awaiting_approval')]
                if blocked:
                    evidence['slice_blockers'] = blocked
                    save()
                    raise RuntimeError('native Pass slice blocked; inspect durable slice evidence')
            if (args.verify_pauses and service.pause_status(value)['boundary']=='exact_plan'
                    and value.get('pause_control', {}).get('evidence_digest')):
                value = quiescent_pause('exact_plan')
                assert value['batch_id'] and value['dispatch_policy']['mode']=='disabled'
                assert not list((vault/'_system/knowledge-builds').glob('task-*/passes/*.json'))
                ledger_before = service._path(args.workflow_id).read_bytes()
                recovered = dispatch(vault, 'resume', request(value))
                assert not recovered['background_dispatch']
                assert service._path(args.workflow_id).read_bytes()==ledger_before
                assert all(c['state'] not in ('running','done') for c in cards
                           if c['node'].startswith('pass-slice:'))
                evidence['exact_plan_pause_verified'] = True
                continuation = request(value, continue_id='gate-continue-plan', boundary='exact_plan',
                    evidence_digest=value['pause_control']['evidence_digest'])
                next_dispatch = dispatch(vault, 'continue-workflow', continuation)
                watcher_pid = next_dispatch.get('reconciler_pid', watcher_pid)
                evidence['explicit_plan_continuation'] = continuation
                save()
                continue
            canary = vault/f'_system/ledgers/ingest-workflows/{args.workflow_id}/reports/canary.json'
            if canary.exists():
                outcome = json.loads(canary.read_text())
                assert outcome['ok'] and outcome['execution_mode']==('auto_full' if args.verify_pauses else 'canary_only')
                assert len(set(outcome['slice_ids']))==8
                snapshots = [native.task_snapshot(value['kanban']['board_id'],c['task_id'])
                             for c in value['kanban']['task_map']
                             if c['node'].startswith('pass-slice:') and c['node'].partition(':')[2] in outcome['slice_ids']]
                if any(s['task']['status']!='done' for s in snapshots):
                    time.sleep(3)
                    continue
                plan_path = next((vault/'_system/reports/exact-planning').glob('*/plan-request.json'))
                plan = json.loads(plan_path.read_text())
                refs = [json.dumps(r,sort_keys=True) for t in plan['tasks'] for r in t['target_refs']]
                assert len(refs)==len(set(refs))
                expected = sum(len(service.knowledge.source.list(o['resource_id'],o['unit_set_id']))
                               for o in value['source_outcomes'] if o['status']=='ready')
                assert len(refs)==expected
                measurements = [json.loads(p.read_text()) for p in (plan_path.parent/'measurements').glob('*.json')]
                by_refs = {json.dumps(m['refs'],sort_keys=True):m['measurement'] for m in measurements}
                assert all(by_refs[json.dumps(t['target_refs'],sort_keys=True)]['fits'] and
                           by_refs[json.dumps(t['target_refs'],sort_keys=True)]['limit']==12000 for t in plan['tasks'])
                selected = [service.knowledge._slice(value['batch_id'],sid) for sid in outcome['slice_ids']]
                assert all(s['state']=='completed' and s['result_refs'] for s in selected)
                if args.verify_preflight:
                    evidence['native_preflight'] = native_preflight_evidence(home, snapshots)
                    evidence['native_preflight_verified'] = True
                if args.verify_pauses:
                    value = quiescent_pause('canary')
                    assert service.pause_status(value)['boundary']=='canary'
                    assert value['dispatch_policy']['mode']=='canary'
                    assert evidence.get('exact_plan_pause_verified')
                    assert not dispatch(vault, 'resume', request(value))['background_dispatch']
                    for c in value['kanban']['task_map']:
                        if c['node'].partition(':')[2] not in outcome['slice_ids']:
                            assert native.task_snapshot(value['kanban']['board_id'],c['task_id'])['task']['status'] not in ('running','done')
                    evidence['canary_pause_verified'] = True
                else:
                    assert all(c['node'].startswith('pass-slice:') for c in value['kanban']['task_map'])
                assert not list((vault/'_system/knowledge-builds').glob('*/reduce.json'))
                # Observe a further dispatch period: completed canary must not promote.
                time.sleep(min(60, float(config['kanban'].get('dispatch_interval_seconds',60))))
                after = service.status(args.workflow_id)
                assert after['dispatch_policy']==value['dispatch_policy']
                assert after['kanban']['task_map']==value['kanban']['task_map']
                evidence.update(canary=outcome, native_snapshots=snapshots,
                    unique_core_units=len(refs), tasks=len(plan['tasks']),
                    all_task_windows_fit=True, budget=12000, ready_sources=len(outcome['source_coverage']['ready']),
                    failed_sources=len(outcome['source_coverage']['failed']), passed=True)
                assert candidate_hashes(repo)==fixed_hashes==candidate_hashes(home/'skills/domain')
                print('GATE_PASSED', flush=True)
                break
            time.sleep(5)
        else:
            raise RuntimeError('full native canary acceptance exceeded observation bound')
    except BaseException as exc:
        import traceback
        evidence['passed'] = False
        evidence['error']=repr(exc)
        evidence['traceback']=traceback.format_exc()
        print('GATE_FAILED',str(exc),flush=True)
        # Preserve only Pass draft JSON, never credentials or whole conversations.
        drafts = []
        for path in home.rglob('*.json'):
            try:
                payload = json.loads(path.read_text())
                if isinstance(payload, dict) and payload.get('batch_id') and isinstance(payload.get('passes'), list):
                    destination = sandbox/'failed-pass-drafts'/f'{sha(path)}.json'
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
                    drafts.append(str(destination))
            except (OSError, ValueError):
                pass
        evidence['failed_pass_drafts'] = sorted(set(drafts))
    finally:
        # Only terminate this isolated runtime; never stop the user's Gateway.
        if not evidence['passed'] and evidence.get('resume'):
            try:
                current = service.status(args.workflow_id)
                if not current['cancel_requested']:
                    current = service.cancel(request(current))
                for card in current['kanban']['task_map']:
                    native.archive(current['kanban']['board_id'], card['task_id'])
            except Exception as cleanup_error:
                evidence['cleanup_error'] = repr(cleanup_error)
        if gateway is not None and gateway.poll() is None:
            gateway.terminate()
            try: gateway.wait(timeout=20)
            except subprocess.TimeoutExpired: gateway.kill(); gateway.wait()
        if watcher_pid:
            proc = Path(f'/proc/{watcher_pid}/cmdline')
            if proc.exists() and str(vault).encode() in proc.read_bytes():
                import signal
                os.kill(watcher_pid,signal.SIGTERM)
        (home/'auth.json').unlink(missing_ok=True)
        evidence['original_ledger_unchanged'] = original_ledger.read_bytes()==before
        evidence['original_raw_unchanged'] = all(sha(original/p)==digest for p,digest in raw.items())
        if not evidence['original_ledger_unchanged'] or not evidence['original_raw_unchanged']:
            evidence['passed'] = False
        evidence['finished_at']=time.time()
        save()
    return 0 if evidence['passed'] else 2


if __name__=='__main__':
    raise SystemExit(main())
