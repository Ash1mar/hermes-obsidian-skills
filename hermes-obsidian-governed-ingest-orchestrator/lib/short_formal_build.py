"""Authenticate saved short-path fragments and hand them to formal Build."""
from contextlib import closing
import os
from pathlib import Path
import re

from hermes_source_units import ContractError
from hermes_source_units.build_acceptance import assess, record
from hermes_source_units.page_governance import POLICY
from hermes_source_units.short_build import admit, evidence, identity, qa_advisory, record_qa_advisory
from hermes_source_units.source_units import _load_json, _sha, _vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from short_evidence_pass import mapping_groups, require, seed_requests
from short_fragment_disposition import delivery_partition
from short_page_contract import auditable_qa, check_fragment


def native_fragment(root, receipt, attempts, conn, runner):
    from fixed_semantic_recovery import transcript_reply
    from hermes_cli import kanban_db as kb

    request_id = receipt['request_sha256']
    slot = receipt['slot']
    for pointer in attempts.get(request_id, []):
        job, reservation, audit_path = (_vault_path(root, pointer[k]) for k in ('job', 'reservation', 'audit'))
        prior = _load_json(job)['request']
        reserved, audit = _load_json(reservation), _load_json(audit_path)
        call = reserved['request']
        if slot not in call['view']['pages'] or call['phase'] != 'short-write':
            continue
        if call['id'] != prior['id']:
            if not runner.checked_retry(prior, call, job):
                continue
        grant = audit['grant']
        native = kb.get_task(conn, grant['task_id'])
        completion = job.with_suffix('.result.json')
        if (not native or native.created_by != runner.CONTRACT or native.body != runner.task_body(root, job)
                or grant['board'] != _load_json(root / 'operation.json')['board']
                or audit['grant'] != reserved['grant'] or audit['request_id'] != call['id']
                or audit['ordinal'] != reserved['ordinal'] or not completion.is_file()):
            continue
        result = _load_json(completion)
        if (native.status != 'done' or result['task_id'] != native.id or result['run_id'] != grant['run_id']
                or result['request_id'] != prior['id']
                or not any(r.id == grant['run_id'] for r in kb.list_runs(conn, native.id, include_active=False))):
            continue
        projection = runner.checked_projection(call)
        model = audit['model']
        if model['parent_native_run_id'] != str(grant['run_id']) or not model['fixed_request_checks']:
            continue
        proof = {'model': model, 'response_sha256': fingerprint(audit['raw']),
                 'user_sha256': fingerprint(projection['wire']['user']),
                 'image_inputs': [{k: v for k, v in img.items() if k != 'url'} for img in call['image_inputs']]}
        if transcript_reply(proof, projection['wire']) != audit['raw']:
            continue
        try:
            answer, _ = runner.decode_semantic_response(audit['raw'])
            if slot not in answer['results'] or check_fragment(prior, slot, answer['results'][slot]) != receipt:
                continue
        except (ValueError, TypeError, ContractError):
            continue
        require(all(runner.sha(path) == pointer[key + '_sha256'] for key, path in
                    (('job', job), ('reservation', reservation), ('audit', audit_path))),
                'native fragment inputs changed during verification')
        return prior['view']['pages'][slot], {**pointer, **grant, 'request_id': call['id'],
                'slot': slot, 'session_id': model['session_id'], 'response_sha256': proof['response_sha256'],
                'completion_sha256': runner.sha(completion), 'receipt_sha256': receipt['receipt_sha256']}
    raise ContractError('SHORT_FRAGMENT_PROOF_MISSING', '$',
                        f'no acknowledged native proof for fragment {receipt["fragment_id"]}')


def final_plan(root, operation, authorization, runner):
    initial = authorization['writing_entry'] == 'initial'
    directory = root if initial else root / 'reentries' / authorization['writing_entry']
    control_path = directory / ('operation.json' if initial else 'control.json')
    result_path = directory / 'result.json'
    control, result = _load_json(control_path), _load_json(result_path)
    require((initial or (control['operation_sha256'] == runner.sha(root / 'operation.json')
            and (control['from_node'] == 'writing' or control.get('mapping_recovery'))))
            and control['page_contract'] == runner.FRAGMENT_CONTRACT
            and result['last_stage'] == 'offline_build' and not result['native_active_or_ready']
            and result['origin'] == operation['origin'], 'a settled writing checkpoint is required')
    origin = _load_json(Path(operation['origin']['path']))
    require(origin['actor'] == authorization['actor'], 'formal actor must own the source boundary')
    controls = [_load_json(p) for p in (root / 'reentries').glob('*/control.json')]
    current = max((c for c in controls if c.get('page_contract') and
                   c['operation_sha256'] == runner.sha(root / 'operation.json')),
                  key=lambda c: c['started_at'], default=operation)
    require(current == control, 'writing checkpoint is superseded by another reentry')
    saved_plan = sorted(directory.glob('stage-*-offline_build.json'))[-1]
    require(authorization['checkpoint'] == {
        'control_sha256': runner.sha(control_path), 'result_sha256': runner.sha(result_path),
        'plan_sha256': runner.sha(saved_plan)}, 'authorized writing checkpoint changed')
    if control.get('fragment_exclusions'):
        pin = control['fragment_exclusions']['evidence']
        require(runner.sha(_vault_path(root.parent, pin['ref'])) == pin['sha256'],
                'reviewed fragment exclusion evidence changed')
    origin = operation['origin']
    sources, live = runner.source_boundary(Path(operation['vault']), origin['workflow_id'], operation['pins'],
                                           control.get('registry_revision') if control.get('mapping_recovery') else None)
    require(live == origin, 'current post-MinerU origin differs from the native writing origin')
    mapping_contract = operation.get('mapping_contract')
    overlay = (root / 'reentries' / control['mapping_overlay_entry'] if control.get('mapping_overlay_entry')
               else directory) / 'mapping-receipts' if control.get('mapping_recovery') else None
    if overlay:
        for path in overlay.glob('*.json'):
            record = _load_json(path)
            proof = record['native_replay']
            require(path.stem in control['mapping_recovery']['requests']
                    and all(runner.sha(_vault_path(root, proof[key])) == proof[key + '_sha256']
                            for key in ('job', 'reservation', 'audit')),
                    'historical mapping proof changed before formal Build')
    rebuilt = runner.advance(sources, seed_requests(sources), mapping_groups(sources, mapping_contract), root / 'receipts',
                             operation['body_limit'], native_holds=result['execution_holds'],
                             page_contract=control['page_contract'], fragment_repair=control.get('fragment_repair'),
                             fragment_exclusions=control.get('fragment_exclusions'), mapping_contract=mapping_contract,
                             mapping_overlay=overlay, preserved_pages=control.get('preserved_pages', ()),
                             writing_contract=operation.get('writing_contract'))
    require(rebuilt == _load_json(saved_plan), 'formal Build requires reproducible writing, QA and coverage')
    return rebuilt, sources


def collect(root, operation, authorization, runner, *, dependencies=None):
    plan, sources = final_plan(root, operation, authorization, runner)
    partition = delivery_partition(plan)
    chosen = authorization['pages']
    require(chosen and sorted(set(chosen)) == chosen and set(chosen) <= set(partition['included']),
            'formal Build selection includes a held or duplicate page')
    service = FileVaultFinalizeService(operation['vault'])
    current = service._current_sets()
    governance = {r['resource_id']: r for r in service._source_eligibility(current, {}) if r['resource_id']}
    refs = {ref: item['source_ref'] for source in sources.values() for ref, item in source['items'].items()}
    by_hash = {r['receipt_sha256']: r for r in plan['checks']}
    coverage = {r['coverage_sha256']: r for r in plan.get('fragment_repair', {}).get('coverage_receipts', [])}
    attempts = runner.attempt_index(root)
    _, kc, _ = runner.runtime()
    candidates, bodies, proof_holds = [], {}, {}
    with closing(kc.connect(board=operation['board'])) as conn:
        for page_id in chosen:
            page = plan['assembled_sections'][page_id]
            content = page['content']
            cited = sorted(set(re.findall(r'\[(S[0-9]+-[AB][0-9]+)\]', content)))
            support = [refs[ref] for ref in cited]
            proof, qa, checks = [], {}, []
            for receipt in page['fragment_receipts']:
                try:
                    view, native = native_fragment(root, by_hash[receipt], attempts, conn, runner)
                except ContractError as error:
                    if error.code != 'SHORT_FRAGMENT_PROOF_MISSING':
                        raise
                    proof_holds[page_id] = {'category': 'unresolved', 'reason': str(error),
                                            'reopen_condition': 'recover the exact acknowledged native fragment'}
                    break
                proof.append(native)
                if dependencies is not None:
                    inspected = set(by_hash[receipt]['answer']['inspected_assets'])
                    assets = {ref for ref, duty in view['obligations'].items() if duty['inspect']}
                    require(assets == inspected, 'verified fragment lacks its required whole-asset inspection')
                    checks.append({'fragment_id': view['fragment_id'], 'receipt_sha256': receipt,
                                   'materials': sorted(view['materials']),
                                   'required_expressions': sorted(ref for ref, duty in view['obligations'].items()
                                                                  if duty['express']),
                                   'required_assets': {ref: sources[ref.split('-', 1)[0]]['items'][ref]['sha256']
                                                       for ref in sorted(assets)},
                                   'source_revisions': view['source_revisions'],
                                   'registered_packet_and_coverage': 'verified'})
                for q in auditable_qa(view):
                    if q['publication'] != 'allow':
                        qa[q['id']] = q
            if page_id in proof_holds:
                continue
            if dependencies is not None:
                dependencies[page_id] = checks
            require(sorted(qa) == page['qa_publication_review'], 'formal QA differs from verified writing packets')
            current_support = bool(support) and all(service._contribution(r, current, {}) == 'active'
                and governance.get(r['unit_ref']['resource_id'], {}).get('eligible') for r in support)
            bodies[page_id] = content.encode('utf-8')
            candidates.append({'short_id': page_id, 'name': page['name'], 'authored_sha256': _sha(bodies[page_id]),
                               'support_refs': support, 'citation_bindings': {key: refs[key] for key in cited},
                               'native_fragments': proof,
                               'same_page_coverage': [coverage[key] for key in page.get('coverage_receipts', [])],
                               'qa_issues': list(qa.values()), 'qa_status': 'qa_required' if qa else 'usable',
                               'source_eligible_at_admission': current_support,
                               'publication_authorized': authorization['publish'] and current_support and not qa})
    exclusions = {**partition['excluded'], **proof_holds}
    excluded = [{'short_id': key, 'name': plan['expected_pages'][key]['name'],
                 'disposition': exclusions.get(key, {'category': 'delivery_conditions',
                     'reason': 'outside-exact-short-build-selection', 'reopen_condition': 'authorize the exact page'})}
                for key in sorted(plan['expected_pages'].keys() - bodies.keys())]
    return service, {
        'origin': operation['origin'], 'actor': authorization['actor'], 'authorization': authorization,
        'operation_sha256': runner.sha(root / 'operation.json'), 'coverage_complete': False,
        'pages': candidates, 'excluded': excluded, 'fragment_exclusions': plan.get('fragment_exclusions', []),
        'mapping_gaps': plan['reduced']['unresolved'], 'coordination_gaps': plan['coordination_issues'],
        'mapping_holds': plan['held_repairs'],
        **({'mapping_receipts': plan['reduced']['mapping_receipts']} if 'mapping_receipts' in plan['reduced'] else {}),
    }, bodies


def build(root, job, request, own, runner):
    operation = _load_json(root / 'operation.json')
    authorization = request['view']['authorization']
    require(request['id'] == fingerprint(request['view']), 'native formal request identity changed')
    dependencies = {}
    service, scope, bodies = collect(root, operation, authorization, runner, dependencies=dependencies)
    if request['phase'] == 'short-formal-qa':
        run = _load_json(service.build._run_path(request['view']['run_id']))
        require(run['state'] == 'completed' and evidence(service.build, run)['scope'] == scope,
                'release reentry must reproduce the existing committed Build and all exclusions')
        service.build.validate_run(run['run_id'])
        own()
        record_qa_advisory(service.build, run, dependencies, root, job)
        return {'state': 'settled', 'attempts': [], 'formal_run_id': run['run_id']}
    if not scope['pages']:
        return {'state': 'settled', 'attempts': [], 'formal_holds': scope['excluded']}
    own()
    run = admit(service.build, scope, bodies, root, job)
    if run['state'] == 'draft':
        reviews = [{'page_id': p['page_id'], 'authored_sha256': p['authored_sha256'], 'parent_authored_sha256': None,
                    'actor': run['actor'], 'acceptance': record(assess(service.build, run, p)),
                    'note': 'Verified exact native fragments, current source bindings, and scoped QA; independent review not performed.'}
                   for p in run['page_revisions']]
        own()
        run = service.build.finalize({'run_id': run['run_id'], 'actor': run['actor'],
                                     'expected_revision': run['revision'], 'reviews': reviews})['run']
    service.build.validate_run(run['run_id'])
    own()
    record_qa_advisory(service.build, run, dependencies, root, job)
    return {'state': 'settled', 'attempts': [], 'formal_run_id': run['run_id']}


def delivery(vault, run_id, authorization):
    service = FileVaultFinalizeService(vault)
    run = _load_json(service.build._run_path(run_id))
    proof = evidence(service.build, run)
    advisory_pointer, advisory = qa_advisory(service.build, run)
    require(proof['scope']['authorization'] == authorization, 'publication authorization differs from Build')
    service.build.validate_run(run_id)
    state = service.status()
    release_id = 'release-short-' + fingerprint({'run_id': run_id, 'base_release': authorization['base_release']})[:24]
    request = {'release_id': release_id, 'actor': run['actor'], 'expected_state_revision': state['revision'],
               'build_run_ids': [run_id], 'reason': 'Exact short-path partial delivery; retain prior published pages and all gaps.'}
    if state['current_release_id'] == release_id:
        release = _load_json(service._manifest_path(release_id))
        service.validate(release_id)
        plan = _load_json(service._plan_path(release_id))
    else:
        require(state['current_release_id'] == authorization['base_release'], 'base release changed before delivery')
        plan = service._build_plan(request)
        release = None
        if authorization['publish'] and not plan['blockers']:
            if authorization['base_release']:
                base = _load_json(service._manifest_path(authorization['base_release']))
                previous = {p['id'] for p in base['index_eligibility'] if p['kind'] == 'knowledge_page' and p['eligible']}
                qualified = {p['id'] for p in plan['index_eligibility'] if p['eligible']}
                require(previous <= qualified, 'additive delivery would displace previously qualified pages')
            service.plan(request)
            release = service.apply({'release_id': release_id, 'actor': run['actor'],
                                     'plan_id': plan['plan_id'], 'expected_revision': plan['revision']})['release']
            service.validate(release_id)
    released = {r['page_id'] for r in release['subjects']} if release else set()
    exclusions = {r['page_id']: r for r in plan['delivery']['excluded']}
    candidates = []
    for row, page in zip(proof['scope']['pages'], run['page_revisions'], strict=True):
        require(identity(service.build, row['short_id'], row['name'])[1] == page['page_id'], 'formal result page changed')
        candidates.append({'name': row['name'], 'short_id': row['short_id'], 'page_id': page['page_id'],
                           'path': page['path'], 'revision_id': page['revision_id'], 'qa_status': page['qa_status'],
                           'qa_issues': row['qa_issues'], 'build_acceptance': page['review']['acceptance'],
                           'qa_disposition': next((q for q in (advisory or {}).get('pages', [])
                                                   if q['page_id'] == page['page_id']), None),
                           'coverage_proofs': row['same_page_coverage'], 'published': page['page_id'] in released,
                           'exclusion': exclusions.get(page['page_id'])})
    return {'run_id': run_id, 'build_revision': run['revision'], 'build_evidence': run['short_evidence'],
            'qa_advisory': advisory_pointer,
            'candidates': candidates, 'isolated_pages': proof['scope']['excluded'],
            'release_id': release['release_id'] if release else None, 'base_release': authorization['base_release'],
            'blockers': plan['blockers'], 'model_calls': 0}


def execute(root, entry, authorization, dry_run, runner, *, existing_build=None):
    require(not os.environ.get('HERMES_KANBAN_TASK') and not os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'),
            'trusted operator must authorize formal short Build')
    require(re.fullmatch(r'formal-[a-z0-9][a-z0-9-]{0,39}', entry)
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', authorization['writing_entry'])
            and set(authorization) == {'contract', 'writing_entry', 'checkpoint', 'actor', 'pages',
                                        'base_release', 'publish', 'policy'}
            and authorization['contract'] == 'short-formal-authorization/v1'
            and type(authorization['publish']) is bool and authorization['policy'] == POLICY,
            'formal short Build requires exact operator authorization and policy')
    with runner.exclusive_lock(root / 'controller.lock', timeout=0, marker=False):
        operation = _load_json(root / 'operation.json')
        kb, kc, _ = runner.runtime()
        require(not runner.workflow_processes(operation['origin']['workflow_id'], operation['board']),
                'formal admission requires a drained board')
        with closing(kc.connect(board=operation['board'])) as conn:
            require(all(t.status not in ('ready', 'running', 'todo', 'triage') and not t.claim_lock
                        for t in kb.list_tasks(conn)), 'formal admission requires no outstanding native claims')
        service = FileVaultFinalizeService(operation['vault'])
        state = service.status()
        directory = root / 'reentries' / entry
        result_path = directory / 'result.json'
        if result_path.exists():
            return _load_json(result_path)
        view = {'entry': entry, 'authorization': authorization}
        if existing_build:
            require(re.fullmatch(r'short-build-[0-9a-f]{64}', existing_build), 'invalid existing short Build identity')
            existing = _load_json(service.build._run_path(existing_build))
            require(existing['state'] == 'completed'
                    and evidence(service.build, existing)['scope']['authorization'] == authorization,
                    'release reentry requires exact completed Build authorization')
            view['run_id'] = existing_build
        row = {'id': fingerprint(view), 'phase': 'short-formal-qa' if existing_build else 'short-formal-build', 'view': view}
        job = root / 'jobs' / f'entry-{entry}-{row["id"]}.json'
        control = {'entry': entry, 'operation_sha256': runner.sha(root / 'operation.json'), 'formal_request': row['id']}
        if dry_run:
            _, scope, _ = collect(root, operation, authorization, runner)
            return {'scope': scope, 'expected_run_id': 'short-build-' + fingerprint(scope), 'model_calls': 0}
        if job.with_suffix('.result.json').exists():
            finished = _load_json(job.with_suffix('.result.json'))
            with closing(kc.connect(board=operation['board'])) as conn:
                task = kb.get_task(conn, finished['task_id'])
                require(task.status == 'done' and task.body == runner.task_body(root, job)
                        and any(r.id == finished['run_id'] for r in kb.list_runs(conn, task.id, include_active=False)),
                        'formal native completion is unacknowledged')
        else:
            require(state['current_release_id'] == authorization['base_release'], 'exact base release required')
            runner.save(directory / 'control.json', control)
            finished, = runner.stage(root, operation, [row], 1, entry=entry)
        require(finished['request_id'] == row['id'] and finished['state'] == 'settled',
                'formal native Build held before verified completion')
        result = (delivery(operation['vault'], finished['formal_run_id'], authorization)
                  if 'formal_run_id' in finished else {
                      'candidates': [], 'isolated_pages': finished['formal_holds'], 'release_id': None,
                      'base_release': authorization['base_release'], 'blockers': ['no-native-build-candidates'],
                      'model_calls': 0})
        runner.save(result_path, result)
        return result
