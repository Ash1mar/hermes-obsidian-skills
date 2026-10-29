#!/usr/bin/env python3
"""Real-model, isolated Pass request observation; never native workflow acceptance.

Read verified existing reading packages for ONE PDF. Run production projections,
budget grouping, HermesCaller and output schemas in independent processes. Native
claims, leases, Pass submission and reconciliation are deliberately outside scope.
No formal workflow is started, resumed, changed or completed by this tool.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import MethodType, SimpleNamespace


def load(path):
    from hermes_source_units.workflow_storage import decode
    return decode(Path(path),json.loads(Path(path).read_text(encoding='utf-8')))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def event(root, kind, **fields):
    row = dict(event=kind, at=time.time(), monotonic=time.monotonic(), pid=os.getpid(), **fields)
    with (Path(root) / f'events-{os.getpid()}.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def imports(skill):
    sys.dont_write_bytecode = True
    sys.path.insert(0, '/usr/local/lib/hermes-agent')
    sys.path.insert(0, str(Path(skill) / 'lib'))
    import fixed_semantic
    return fixed_semantic


def protect(vault, skill):
    """Deny Python filesystem mutations of source Vault and installed code."""
    roots = [Path(vault).resolve(), Path(skill).resolve()]
    def forbidden(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        p = Path(os.fsdecode(value)).resolve()
        return any(p == r or p.is_relative_to(r) for r in roots)
    def audit(name, args):
        paths = []
        if name == 'open':
            mode, flags = args[1], args[2]
            if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
                paths = [args[0]]
        elif name in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.truncate', 'os.utime'):
            paths = [args[0]]
        elif name in ('os.rename', 'os.link', 'os.symlink'):
            paths = list(args[:2])
        if any(forbidden(p) for p in paths):
            raise PermissionError('observation cannot mutate source Vault or installed Skill')
    sys.addaudithook(audit)


def formal_snapshot(vault):
    # Audit evidence only, no copies of old code or domain payloads.
    return {p.relative_to(vault).as_posix(): sha(p) for p in sorted(Path(vault).rglob('*'))
            if p.is_file() and not p.is_symlink() and p.suffix in ('.json', '.md', '.pdf')}


def quiet(vault):
    workflows = Path(vault) / '_system/ledgers/ingest-workflows'
    for p in workflows.glob('*.json'):
        d = load(p)
        if d.get('state') not in ('cancelled', 'completed', 'failed') and not d.get('cancel_requested'):
            raise RuntimeError('formal workflow is not stopped: ' + p.stem)
    for p in Path('/proc').iterdir():
        if not p.name.isdigit() or p.name == str(os.getpid()):
            continue
        try:
            argv = (p / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        names = {Path(os.fsdecode(a)).name for a in argv if a}
        if names & {'run_program_worker.py', 'run_fixed_worker.py', 'watch_ingest_workflow.py'}:
            raise RuntimeError('another ingest consumer is active: ' + p.name)


def prepare(args):
    fixed = imports(args.skill)
    from hermes_source_units.knowledge_build import FileKnowledgeBuildService
    from hermes_source_units.validation import fingerprint
    from native_pass_identity import visible_packet
    from fixed_semantic_media import image_catalog
    protect(args.vault, args.skill)
    if not args.prepare_only:
        quiet(args.vault)
    root = Path(args.output).resolve()
    if root.exists():
        raise RuntimeError('output must be new; existing attempts are never replayed')
    if root.is_relative_to(Path(args.vault).resolve()):
        raise RuntimeError('output must be outside source Vault')
    root.mkdir(parents=True)
    before = formal_snapshot(Path(args.vault))
    write(root / 'formal-before.json', before)
    registry = load(Path(args.vault) / '_system/metadata/document-registry.json')
    source_hash = sha(args.source)
    selected = [r for r in registry['records'] if r['content_sha256'] == source_hash]
    if len(selected) != 1:
        raise RuntimeError('source must match exactly one registered document')
    record = selected[0]
    if record['processing_status'] != 'completed':
        raise RuntimeError('source preparation is not complete')
    service = FileKnowledgeBuildService(args.vault)
    batch = service._batch(args.batch)
    projections, originals = [], []
    for tid in batch['task_ids']:
        task = service._task(tid)
        resources = {r['unit_ref']['resource_id'] for r in task['target_refs']}
        if record['resource_id'] not in resources:
            continue
        if resources != {record['resource_id']}:
            raise RuntimeError('cross-source task is not eligible for single-PDF observation')
        package = service._existing_reading_package(task, batch['actor'], batch['document_registry_revision'])
        if package is None:
            raise RuntimeError('missing existing reading package: ' + tid)
        projection = service.model_input(tid, package, persist=False)
        if any(m['unit_ref']['resource_id'] != record['resource_id'] for m in projection['mapping'].values()):
            raise RuntimeError('cross-source context is not allowed')
        # Experimental work starts from evidence only; historical semantic answers
        # remain untouched and are never sent as this observation's new candidates.
        projection['view']['continuation'] = {'action': 'candidate_then_citation'}
        projection['view']['next_sequence'] = 0
        projections.append(projection)
        originals.append({'task_id': tid, 'package_id': package['package_id'],
                          'input_id': projection['input_id'], 'original_status': task['status'],
                          'core_refs': task['target_refs']})
    if not projections:
        raise RuntimeError('no verified source tasks')
    adapter = SimpleNamespace(workflow=SimpleNamespace(vault=Path(args.vault), knowledge=service))
    jobs = []
    for start in range(0, len(projections), args.tasks_per_job):
        chosen = projections[start:start + args.tasks_per_job]
        packet = visible_packet(service.model_packet(chosen, 'isolated-pass-observation', persist=False)['view'])
        images = image_catalog(adapter, packet)
        groups, plan = fixed.build_group_plan(packet, packet['tasks'], 'candidate', 30000,
            images, fixed.PHASE_BALANCED_POLICY, pinned=False)
        if plan['state'] != 'ready':
            raise RuntimeError('complete experimental packet exceeds budget: ' + json.dumps(plan['error']))
        job = {'job': len(jobs) + 1, 'packet': packet, 'images': images,
               'original_tasks': [p['task_id'] for p in chosen],
               'mapping': {f't{i}': p['mapping'] for i, p in enumerate(chosen, 1)},
               'candidate_plan': plan}
        path = root / 'inputs' / f'job-{job["job"]:03d}.json'
        write(path, job)
        jobs.append({'path': str(path), 'sha256': sha(path), 'tasks': len(chosen)})
    code = {p.relative_to(args.skill).as_posix(): sha(p) for p in Path(args.skill).rglob('*')
            if p.is_file() and p.suffix in ('.py', '.json', '.md')}
    manifest = {'contract': 'isolated-pass-request-observation/v1', 'created_at': time.time(),
        'scope': 'real candidate/citation requests; excludes native dispatch, leases and Pass persistence',
        'vault': str(Path(args.vault).resolve()), 'source': str(Path(args.source).resolve()),
        'source_sha256': source_hash, 'resource_id': record['resource_id'], 'batch': args.batch,
        'task_count': len(projections), 'jobs': jobs, 'tasks_per_job': args.tasks_per_job,
        'concurrency': args.concurrency, 'deadline_seconds': args.seconds,
        'candidate_sha': args.candidate_sha, 'runner_sha256': sha(__file__),
        'task_origins': originals, 'skill': str(Path(args.skill).resolve()), 'code_fingerprints': code,
        'formal_results_imported': False, 'automatic_corrections': False,
        'new_formal_passes': 0, 'mineru_executed': False}
    write(root / 'manifest.json', manifest)
    after = formal_snapshot(Path(args.vault))
    changes = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    manifest['formal_changes_during_preparation'] = changes
    manifest['offline_only'] = bool(args.prepare_only)
    write(root / 'manifest.json', manifest)
    if changes and not args.prepare_only:
        raise RuntimeError('formal state changed during preparation; do not start')
    # Offline material inspection may coexist with an unrelated active reducer.
    # It grants no permission to execute: run() always prepares afresh and checks
    # stopped state, while every actual SDK call rechecks that condition.
    return root, manifest


def validate_evidence(fixed, item, task, mapping):
    """Mechanical scope/coverage checks; never a claim of semantic correctness."""
    from hermes_source_units.model_projection import ModelProjectionMixin
    from hermes_source_units.validation import fingerprint
    from hermes_source_units.knowledge_build import _covers
    expanded = []
    for inspection in item['inspections']:
        expanded.append(ModelProjectionMixin._expand_model_ref(inspection['source_ref'], mapping))
    if len({fingerprint(r) for r in expanded}) != len(expanded):
        raise ValueError('duplicate inspections')
    for material in task['materials']:
        if material['role'] == 'core' or material.get('required_dependency'):
            if not any(_covers(r, mapping[material['ref']]) for r in expanded):
                raise ValueError('required material not inspected: ' + material['ref'])
    ids = [c['candidate_id'] for c in item['candidates']]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate candidate IDs')
    if not ids and not item['empty_reason'].strip():
        raise ValueError('empty candidates lack reason')
    for c in item['candidates']:
        for ref in c['support_refs']:
            value = ModelProjectionMixin._expand_model_ref(ref, mapping)
            if not any(_covers(r, value) for r in expanded):
                raise ValueError('candidate cites uninspected material')


def merged_review(prior, review):
    if review['decision'] == 'confirmed_unchanged':
        return copy.deepcopy(prior)
    from hermes_source_units.validation import fingerprint
    updates = {c['candidate_id']: c for c in review['candidates']}
    removed = set(review['removed_candidate_ids'])
    existing = {c['candidate_id'] for c in prior['candidates']}
    if len(updates) != len(review['candidates']) or not removed <= existing or removed & updates.keys():
        raise ValueError('invalid citation delta')
    result = copy.deepcopy(prior)
    result['candidates'] = [updates.pop(c['candidate_id'], c) for c in prior['candidates'] if c['candidate_id'] not in removed]
    result['candidates'].extend(updates.values())
    changes = {fingerprint(i['source_ref']): i for i in review['inspections']}
    if len(changes) != len(review['inspections']):
        raise ValueError('duplicate citation inspections')
    result['inspections'] = [changes.pop(fingerprint(i['source_ref']), i) for i in prior['inspections']]
    result['inspections'].extend(changes.values())
    result['empty_reason'] = review['empty_reason'] if not result['candidates'] else ''
    return result


def child(args):
    root = Path(args.output)
    manifest = load(root / 'manifest.json')
    fixed = imports(manifest['skill'])
    protect(manifest['vault'], manifest['skill'])
    import hermes_state
    # Hermes explicitly supports redirecting DEFAULT_DB_PATH. No config/auth,
    # Kanban or Gateway is created; ordinary model transcript storage is local.
    hermes_state.DEFAULT_DB_PATH = root / f'sessions-{os.getpid()}.db'
    from fixed_semantic_schema import slot_schema, slot_results
    from fixed_semantic_media import select_images
    from hermes_source_units.runtime_telemetry import span
    job = load(args.job)
    if sha(args.job) != next(j['sha256'] for j in manifest['jobs'] if j['path'] == args.job):
        raise RuntimeError('job input changed')
    packet = job['packet']
    out = root / 'results' / f'job-{job["job"]:03d}'
    out.mkdir(parents=True, exist_ok=False)
    caller = fixed.HermesCaller()
    original_make = caller.make_agent
    def make():
        agent = original_make()
        original_api = agent._fixed_api
        def api(_self, call, *aa, **kw):
            # Some provider transports nest guarded API methods. Count the
            # outer request interval once, not each nested implementation frame.
            depth = getattr(agent, '_probe_api_depth', 0)
            agent._probe_api_depth = depth + 1
            if depth:
                try:
                    return original_api(call, *aa, **kw)
                finally:
                    agent._probe_api_depth -= 1
            call_id = agent.session_id + '-' + str(time.monotonic_ns())
            event(root, 'sdk_start', job=job['job'], call_id=call_id, session_id=agent.session_id)
            error = None
            try:
                return original_api(call, *aa, **kw)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                event(root, 'sdk_end', job=job['job'], call_id=call_id, session_id=agent.session_id, error=error)
                agent._probe_api_depth -= 1
        agent._fixed_api = MethodType(api, agent)
        return agent
    caller.make_agent = make
    states = {t['task']: {'status': 'pending'} for t in packet['tasks']}
    write(out / 'status.json', states)
    count = 0
    for phase in ('candidate', 'citation'):
        tasks = [t for t in packet['tasks'] if states[t['task']]['status'] == ('pending' if phase == 'candidate' else 'candidate_valid')]
        if not tasks:
            continue
        groups, plan = fixed.build_group_plan(packet, tasks, phase, 30000, job['images'], fixed.PHASE_BALANCED_POLICY, pinned=False)
        write(out / (phase + '-groups.json'), plan)
        if plan['state'] != 'ready':
            for t in tasks:
                states[t['task']].update(status='budget_held', error=plan['error'])
            write(out / 'status.json', states)
            continue
        for group in groups:
            if (root / 'STOP').exists():
                return
            quiet(manifest['vault'])
            count += 1
            view = fixed.subset(packet, group, phase)
            schema = slot_schema(phase, group)
            images = select_images(group, job['images'])
            prepared = fixed.prepare_request_projection(phase, view, schema, images)
            prepared['audit_context'] = {'vault': str(root), 'binding': {
                'workflow_id': 'isolated-observation', 'task_id': f'probe-job-{job["job"]}'}, 'phase': phase}
            def check():
                if (root / 'STOP').exists():
                    raise RuntimeError('observation stopped before SDK request')
                quiet(manifest['vault'])
            prepared['live_control_check'] = check
            if prepared['input_size'] > 30000:
                raise RuntimeError('request exceeds production budget')
            prefix = out / f'call-{count:02d}-{phase}'
            write(str(prefix) + '-request.json', {'view': view, 'schema': schema, 'wire': prepared['wire'],
                'input_size': prepared['input_size'], 'phase': phase, 'tasks': [t['task'] for t in group]})
            caller.images = images
            event(root, 'call_start', job=job['job'], phase=phase, call=count, tasks=len(group))
            try:
                with span('isolated_pass_call', vault=root, workflow_id='isolated-observation', phase=phase):
                    with caller.request_scope(prepared):
                        raw, metadata = caller(phase, view, schema)
                write(str(prefix) + '-response.json', {'raw': raw, 'metadata': metadata})
                value, _ = fixed.decode_semantic_response(raw)
                decoded = slot_results(phase, value, group)
                write(str(prefix) + '-decoded.json', decoded)
                for failure in decoded['slot_failures']:
                    states[failure['task']].update(status='held', error=failure)
                for item in decoded['drafts' if phase == 'candidate' else 'reviews']:
                    alias = item['task']
                    task = next(t for t in group if t['task'] == alias)
                    try:
                        result = item if phase == 'candidate' else merged_review(states[alias]['candidate'], item)
                        validate_evidence(fixed, result, task, job['mapping'][alias])
                        if phase == 'candidate':
                            states[alias].update(status='candidate_valid', candidate=result)
                            task['continuation'] = {'action': 'citation', 'candidate': {
                                **{k: copy.deepcopy(v) for k, v in result.items() if k != 'task'}, 'sequence': 0}}
                        else:
                            states[alias].update(status='citation_valid', citation=result, review=item)
                    except Exception as exc:
                        states[alias].update(status='held', error=str(exc), original_result=item)
                event(root, 'call_end', job=job['job'], phase=phase, call=count, outcome='returned')
            except Exception as exc:
                write(str(prefix) + '-error.json', {'type': type(exc).__name__, 'message': str(exc)})
                for task in group:
                    states[task['task']].update(status='held', error=str(exc))
                event(root, 'call_end', job=job['job'], phase=phase, call=count, outcome='error', error=type(exc).__name__)
                # Unknown transport outcomes are never automatically resent.
            write(out / 'status.json', states)
    event(root, 'job_end', job=job['job'], states={k: v['status'] for k, v in states.items()})


def summarize(root, finished=False):
    m = load(root / 'manifest.json')
    events = [json.loads(line) for p in root.glob('events-*.jsonl') for line in p.read_text().splitlines() if line]
    starts = {e['call_id']: e for e in events if e['event'] == 'sdk_start'}
    ends = {e['call_id']: e for e in events if e['event'] == 'sdk_end'}
    intervals = [(s['at'], ends[c]['at']) for c, s in starts.items() if c in ends]
    points = sorted([(a, 1) for a, b in intervals] + [(b, -1) for a, b in intervals])
    active = peak = 0
    durations = {}
    previous = points[0][0] if points else 0
    for at, delta in points:
        durations[active] = durations.get(active, 0) + at - previous
        active += delta
        peak = max(peak, active)
        previous = at
    states = [v['status'] for p in (root / 'results').glob('*/status.json') for v in load(p).values()]
    counts = {s: states.count(s) for s in sorted(set(states))}
    summary = {'finished': finished, 'observed_at': time.time(), 'concurrency_limit': m['concurrency'],
        'sdk_peak_completed_intervals': peak, 'sdk_started': len(starts), 'sdk_finished': len(ends),
        'sdk_inflight': len(starts.keys() - ends.keys()), 'seconds_at_concurrency': durations,
        'task_states': counts, 'planned_tasks': m['task_count'], 'jobs': len(m['jobs']),
        'calls': len([e for e in events if e['event'] == 'call_start']),
        'errors': [e for e in events if e['event'] == 'call_end' and e.get('outcome') == 'error'],
        'stop_requested': (root / 'STOP').exists(), 'formal_stage_acceptance': False}
    if intervals:
        total = max(b for a, b in intervals) - min(a for a, b in intervals)
        summary.update(sdk_window_seconds=total, sdk_average_concurrency=sum(b-a for a,b in intervals)/total)
    if finished:
        before = load(root / 'formal-before.json')
        after = formal_snapshot(Path(m['vault']))
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        summary['formal_files_unchanged'] = not changed
        summary['formal_changed_paths'] = changed
        summary['source_unchanged'] = sha(m['source']) == m['source_sha256']
        summary['skill_files_unchanged'] = all(sha(Path(m['skill']) / p) == h for p, h in m['code_fingerprints'].items())
        write(root / 'formal-after.json', after)
    write(root / 'summary.json', summary)
    return summary


def run(args):
    root, manifest = prepare(args)
    pending = list(manifest['jobs'])
    running = []
    started = time.monotonic()
    stop_at = None
    signal.signal(signal.SIGTERM, lambda *_: (root / 'STOP').touch(exist_ok=True))
    signal.signal(signal.SIGINT, lambda *_: (root / 'STOP').touch(exist_ok=True))
    print(json.dumps({'output': str(root), 'tasks': manifest['task_count'], 'jobs': len(pending), 'concurrency': args.concurrency}), flush=True)
    while pending or running:
        if time.monotonic() - started > args.seconds:
            (root / 'STOP').touch(exist_ok=True)
        if (root / 'STOP').exists():
            pending.clear()
            stop_at = stop_at or time.monotonic()
        while pending and len(running) < args.concurrency:
            job = pending.pop(0)
            log = open(root / ('worker-' + Path(job['path']).stem + '.log'), 'w')
            proc = subprocess.Popen([sys.executable, __file__, '--child', '--output', str(root), '--job', job['path']],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            running.append((proc, log, job))
        for proc, log, job in list(running):
            if proc.poll() is not None:
                log.close()
                event(root, 'process_end', worker_pid=proc.pid, returncode=proc.returncode, input=job['path'])
                running.remove((proc, log, job))
                if proc.returncode:
                    (root / 'STOP').touch(exist_ok=True)
            elif stop_at and time.monotonic() - stop_at > 120:
                os.killpg(proc.pid, signal.SIGTERM)
        if stop_at and time.monotonic() - stop_at > 135:
            for proc, _, _ in running:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
        observed = summarize(root)
        if len(observed['errors']) >= 2:
            (root / 'STOP').touch(exist_ok=True)
        if running:
            time.sleep(2)
    print(json.dumps(summarize(root, True), ensure_ascii=False), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--child', action='store_true')
    p.add_argument('--prepare-only', action='store_true', help='verify and freeze inputs without creating a caller or making model requests')
    p.add_argument('--job')
    p.add_argument('--vault')
    p.add_argument('--source')
    p.add_argument('--batch')
    p.add_argument('--skill')
    p.add_argument('--candidate-sha')
    p.add_argument('--output', required=True)
    p.add_argument('--concurrency', type=int, default=5, choices=range(1, 11))
    p.add_argument('--tasks-per-job', type=int, default=3)
    p.add_argument('--seconds', type=int, default=900)
    args = p.parse_args()
    if args.child:
        child(args)
    else:
        if not all((args.vault, args.source, args.batch, args.skill)) or args.tasks_per_job < 1:
            p.error('vault, source, batch, skill and positive tasks-per-job are required')
        if args.prepare_only:
            root, manifest = prepare(args)
            print(json.dumps({'prepared_only': True, 'output': str(root),
                              'tasks': manifest['task_count'], 'jobs': len(manifest['jobs']),
                              'model_calls': 0}), flush=True)
        else:
            run(args)


if __name__ == '__main__':
    main()
