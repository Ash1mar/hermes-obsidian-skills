"""Formal Build ingress and durable evidence for native short-path pages."""
from contextlib import closing
import json
import os
from pathlib import Path

from .partial_finalize import pin, pinned
from .source_units import _exclusive_lock, _json_bytes, _load_json, _sha, _vault_path, _write_atomic
from .validation import ContractError, fingerprint, validate_references

POLICY = 'native-short-build/v1'
ROOT = '_system/knowledge-builds'


def require(test, message):
    if not test:
        raise ContractError('SHORT_BUILD_INVALID', '$', message)


def identity(service, short_id, name):
    vault_id = service.source.vault_id
    key = f'short-evidence:{short_id}'
    subject = fingerprint({'vault_id': vault_id, 'kind': 'concept', 'identity_key': key})
    return {'kind': 'concept', 'identity_key': key, 'canonical_name': name, 'aliases': [], 'subject_id': subject}, (
        fingerprint({'vault_id': vault_id, 'subject_id': subject}))


def output_path(short_id):
    return f'20_Notes/ShortEvidence/{short_id}.md'


def projection(service, row):
    subject, page_id = identity(service, row['short_id'], row['name'])
    page = {'contract': 'hermes-knowledge-page-revision/v1', 'page_id': page_id,
            'subject_id': subject['subject_id'], 'parent_revision_id': None, 'action': 'create',
            'path': output_path(row['short_id']), 'authored_sha256': row['authored_sha256'],
            'support_refs': row['support_refs'], 'qa_status': row['qa_status'],
            'business_status': 'unassessed', 'visibility': 'draft'}
    page['revision_id'] = fingerprint({k: v for k, v in page.items() if k != 'contract'})
    decision = {k: page[k] for k in ('subject_id', 'page_id', 'action', 'path', 'support_refs', 'revision_id')}
    return {**page, 'state': 'draft', 'review': None}, {**decision, 'candidate_refs': [], 'identity': subject}


def native_authority(root, job, operation):
    """Only the process holding this exact short-program claim can admit evidence."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kc

    board = os.environ.get('HERMES_KANBAN_BOARD')
    task_id = os.environ.get('HERMES_KANBAN_TASK')
    run_id = os.environ.get('HERMES_KANBAN_RUN_ID')
    require(board and task_id and run_id and run_id.isdigit() and board == operation['board'],
            'an active native short-path claim is required')
    with closing(kc.connect(board=board)) as conn:
        task = kb.get_task(conn, task_id)
    expected = {'contract': operation['contract'], 'operation_sha256': _sha((root / 'operation.json').read_bytes()),
                'job': job.name, 'job_sha256': _sha(job.read_bytes())}
    require(task and task.created_by == operation['contract'] and task.status == 'running'
            and task.claim_lock and task.claim_lock == os.environ.get('HERMES_KANBAN_CLAIM_LOCK')
            and task.current_run_id == int(run_id) and task.worker_pid == os.getpid()
            and Path(task.workspace_path) == root and json.loads(task.body) == expected,
            'native short Build claim or exact job changed')
    return {'board': board, 'task_id': task_id, 'run_id': int(run_id), 'job_sha256': expected['job_sha256']}


def evidence(service, run):
    pointer = run['short_evidence']
    require(pointer['ref'] == f"{ROOT}/{run['run_id']}/short-evidence.json", 'foreign short evidence pointer')
    record = pinned(service.vault, pointer)
    scope = record['scope']
    require(record['contract'] == POLICY and run['run_id'] == 'short-build-' + fingerprint(scope)
            and record['vault_id'] == service.source.vault_id and scope['actor'] == run['actor']
            and scope['coverage_complete'] is False, 'short evidence scope differs from the Build')
    pages, decisions = zip(*(projection(service, row) for row in scope['pages']), strict=True)
    require(list(decisions) == run['decisions'] and list(pages) == [
        {**page, 'state': 'draft', 'review': None} for page in run['page_revisions']],
        'short evidence differs from formal decisions or revisions')
    return record


def assess(service, run, page):
    from .finalize_revision_state import content_path

    record = evidence(service, run)
    require(_sha(content_path(service, run, page).read_bytes()) == page['authored_sha256'],
            'short Build page bytes changed')
    resources = {r['unit_ref']['resource_id'] for r in page['support_refs']}
    return {'policy': POLICY, 'risk_reasons': ['cross_source_merge'] if len(resources) > 1 else [],
            'short_evidence': run['short_evidence'],
            'evidence_sha256': fingerprint({'record': run['short_evidence'], 'page_id': page['page_id'],
                                           'revision_id': page['revision_id'], 'origin': record['scope']['origin']})}


def publication_approved(service, page, acceptance):
    """Apply the pinned internal retrieval policy without changing page QA."""
    from .page_governance import POLICY as RELEASE_POLICY
    from .build_acceptance import record

    pointer = acceptance.get('short_evidence') or {}
    path = _vault_path(service.vault, pointer.get('ref', ''))
    run = _load_json(path.with_name('manifest.json'))
    proof = evidence(service.build, run)
    require(run['state'] == 'completed' and run['short_evidence'] == pointer
            and page in run['page_revisions'] and acceptance == record(assess(service.build, run, page)),
            'short publication must bind the exact committed Build acceptance')
    row = next(r for r in proof['scope']['pages'] if projection(service.build, r)[0]['page_id'] == page['page_id'])
    authorization = proof['scope']['authorization']
    return (row['publication_authorized'] and page['qa_status'] == 'usable'
            and authorization['publish'] is True and authorization['policy'] == RELEASE_POLICY)


def admit(service, scope, bodies, root, job):
    """Stage drafts under the ordinary Build lock after native proof replay."""
    from .page_governance import POLICY as RELEASE_POLICY

    operation = _load_json(root / 'operation.json')
    admission = native_authority(root, job, operation)
    require(Path(operation['vault']) == service.vault and scope['origin'] == operation['origin']
            and scope['operation_sha256'] == _sha((root / 'operation.json').read_bytes()),
            'short Build admission is outside the exact origin')
    payload = _load_json(job)['request']
    require(payload['phase'] == 'short-formal-build'
            and scope['authorization'] == payload['view']['authorization']
            and scope['pages']
            and {r['short_id'] for r in scope['pages']} <= set(scope['authorization']['pages'])
            and set(scope['authorization']['pages']) <= {r['short_id'] for r in [*scope['pages'], *scope['excluded']]}
            and scope['actor'] == scope['authorization']['actor']
            and scope['authorization']['policy'] == RELEASE_POLICY,
            'short Build admission differs from the operator selection')
    for row in scope['pages']:
        require(row['native_fragments'] and row['support_refs'] and
                row['authored_sha256'] == _sha(bodies[row['short_id']]) and
                row['qa_status'] == ('qa_required' if row['qa_issues'] else 'usable') and
                (not row['publication_authorized'] or (row['qa_status'] == 'usable'
                 and scope['authorization']['publish'] is True)),
                'short Build candidate lacks native, source, or QA evidence')
    run_id = 'short-build-' + fingerprint(scope)
    path = service._run_path(run_id)
    proof_path = path.with_name('short-evidence.json')
    with _exclusive_lock(_vault_path(service.vault, f'{ROOT}/.finalize.lock'), marker=False):
        if path.exists():
            run = _load_json(path)
            require(evidence(service, run)['scope'] == scope, 'short Build recovery inputs changed')
            return run
        registry = service._registry()
        occupied = {s['subject_id'] for s in registry['subjects']}
        pages, decisions = zip(*(projection(service, row) for row in scope['pages']), strict=True)
        require(not any(p['subject_id'] in occupied or _vault_path(service.vault, p['path']).exists()
                        for p in pages), 'short Build cannot overwrite an existing page or identity')
        proof = {'contract': POLICY, 'vault_id': service.source.vault_id, 'scope': scope, 'admission': admission}
        if proof_path.exists():
            old = _load_json(proof_path)
            require(all(old[k] == proof[k] for k in ('contract', 'vault_id', 'scope')),
                    'prior short Build admission differs')
        else:
            _write_atomic(proof_path, _json_bytes(proof))
        run = {'contract': 'hermes-knowledge-build-run/v1', 'run_id': run_id, 'revision': 1, 'state': 'draft',
               'actor': scope['actor'], 'identity_registry_revision': registry['revision'], 'tasks': [],
               'decisions': list(decisions), 'page_revisions': list(pages), 'reviews': [],
               'short_evidence': pin(service.vault, proof_path.relative_to(service.vault).as_posix()),
               'reason': 'native short-path evidence; exact candidate scope with preserved gaps'}
        refs = [r for page in pages for r in page['support_refs']]
        units = service._live_units(refs, run['actor'], service._document_registry_revision())
        validate_references('build_run', run, units)
        for row, page in zip(scope['pages'], pages, strict=True):
            draft = path.parent / 'pages' / f"{page['page_id']}.md"
            if draft.exists():
                require(draft.read_bytes() == bodies[row['short_id']], 'existing short Build draft differs')
            else:
                _write_atomic(draft, bodies[row['short_id']])
        _write_atomic(path, _json_bytes(run))
        return run
