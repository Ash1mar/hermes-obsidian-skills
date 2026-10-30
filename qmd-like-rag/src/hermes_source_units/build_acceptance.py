"""Small, deterministic Build acceptance policy; never a semantic approval."""
from collections import Counter
import copy
import hashlib
import re

from .validation import ContractError
from .source_units import _load_json, _vault_path, _sha, _json_bytes
from .validation import fingerprint, validate_record

CONTRACT = 'program-build-acceptance/v1'
POLICY = 'explicit-page-risks/v1'
ADOPTION = 'build-acceptance-policy-adoption/v1'
MARKER = '<!-- hermes-program-build-acceptance/v1 -->'


def _verify_batch_refresh(vault, proof, evidence):
    """Prove repeated validation changed only the batch's revision counter."""
    def fail():
        raise ContractError('STALE_INPUT', '$', 'checkpoint batch metadata drift lacks exact approved evidence')
    checkpoint = evidence['checkpoints']['checkpoint_1']
    ref = proof['decision_ref']
    if ref != checkpoint.get('decision_ref') or not ref:
        fail()
    raw = _vault_path(vault, ref).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != checkpoint['decision_sha256'] or digest != proof['decision_sha256'] or evidence['files'].get(ref) != digest:
        fail()
    decision = _load_json(_vault_path(vault, ref))
    approved = decision.get('evidence', {}).get('batch')
    if (decision.get('contract') != 'hermes-ingest-checkpoint-decision/v1'
            or decision.get('workflow_id') != evidence['workflow_id'] or decision.get('checkpoint') != 'checkpoint_1'
            or decision.get('approval_digest') != checkpoint['approval_digest']
            or decision.get('ok') is not True or decision.get('blocking_codes') != [] or not approved):
        fail()
    validate_record('knowledge_batch', approved)
    if (proof['path'] != f"_system/ledgers/knowledge-build-batches/{approved['batch_id']}.json"
            or approved['state'] != 'checkpoint_1' or approved['last_operation'] != 'validate' or approved['failures']
            or proof['approved_revision'] != approved['revision'] or proof['paused_revision'] != approved['revision']+1
            or proof['current_revision'] <= proof['paused_revision']):
        fail()
    for field, revision in (('previous_sha256', proof['paused_revision']), ('current_sha256', proof['current_revision'])):
        expected = hashlib.sha256(_json_bytes({**approved, 'revision': revision})).hexdigest()
        if proof[field] != expected:
            fail()
    if evidence['files'].get(proof['path']) != proof['previous_sha256']:
        fail()


def checkpoint_batch_refresh(service, workflow, evidence, actual):
    """Only the evidenced post-checkpoint repeated-validate counter may drift."""
    path = service.knowledge._batch_path(workflow['batch_id']).relative_to(service.vault).as_posix()
    ref = workflow['checkpoints']['checkpoint_1'].get('decision_ref')
    if not ref or path not in evidence['files'] or path not in actual['files']:
        raise ContractError('STALE_INPUT', '$', 'checkpoint evidence changed before adoption')
    approved = _load_json(_vault_path(service.vault, ref)).get('evidence', {}).get('batch', {})
    current = service.knowledge._batch(workflow['batch_id'])
    proof = {'path': path, 'decision_ref': ref, 'decision_sha256': workflow['checkpoints']['checkpoint_1']['decision_sha256'],
             'approved_revision': approved.get('revision'), 'paused_revision': approved.get('revision', -1)+1,
             'current_revision': current['revision'], 'previous_sha256': evidence['files'][path],
             'current_sha256': actual['files'][path]}
    _verify_batch_refresh(service.vault, proof, evidence)
    refreshed = copy.deepcopy(evidence)
    refreshed['files'][path] = proof['current_sha256']
    if refreshed != actual:
        raise ContractError('STALE_INPUT', '$', 'adoption cannot refresh changed domain evidence')
    return proof


def rebase_pause_evidence(evidence, previous_pin, target_pin, batch_refresh=None):
    """Change exactly one template dependency; all domain evidence stays pinned."""
    updated = copy.deepcopy(evidence)
    if updated['files'].pop(previous_pin['path'], None) != previous_pin['template_hash'][7:]:
        raise ContractError('STALE_INPUT', '$', 'old pause does not pin the previous Build template')
    if target_pin['path'] in updated['files']:
        raise ContractError('STALE_INPUT', '$', 'new Build template already occurs in old pause evidence')
    updated['files'][target_pin['path']] = target_pin['template_hash'][7:]
    if batch_refresh:
        updated['files'][batch_refresh['path']] = batch_refresh['current_sha256']
    return updated


def verify_policy_adoption(service, workflow):
    """Read-only audit validation on later resume/dispatch and continuation."""
    pointer = workflow.get('build_acceptance_adoption')
    if pointer is None:
        return
    def fail(message):
        raise ContractError('STALE_INPUT', '$', message)
    prefix = f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}/"
    if pointer['report_ref'] != prefix+'build-acceptance-adoptions/'+pointer['adoption_id']+'.json':
        fail('Build policy adoption report is outside its operation')
    data = _vault_path(service.vault, pointer['report_ref']).read_bytes()
    if hashlib.sha256(data).hexdigest() != pointer['report_sha256']:
        fail('Build policy adoption evidence changed')
    report = _load_json(_vault_path(service.vault, pointer['report_ref']))
    request = report['request']
    from .ingest_workflow import mutation_digest
    old, target = report['previous_pin'], report['target_pin']
    active = next(p for p in workflow['template_pins'] if p['kind'] == 'build-finalize')
    if (report.get('contract') != ADOPTION or request['workflow_id'] != workflow['workflow_id']
            or request['actor'] != workflow['actor'] or request['adoption_scope'] != ADOPTION
            or request['adoption_id'] != pointer['adoption_id'] or request['input_digest'] != pointer['input_digest']
            or mutation_digest(request) != request['input_digest'] or target != active
            or request['previous_pin_digest'] != 'sha256:'+fingerprint(old)
            or request['target_template_hash'] != target['template_hash']
            or report['previous_pause']['history'] != report['new_pause']['history']
            or report['previous_pause']['boundary'] != 'checkpoint_1' or report['new_pause']['boundary'] != 'checkpoint_1'):
        fail('Build policy adoption identity, pins or held boundary changed')
    for pin in (old, target):
        raw = _vault_path(service.vault, pin['path']).read_bytes()
        if pin['kind'] != 'build-finalize' or 'sha256:'+hashlib.sha256(raw).hexdigest() != pin['template_hash']:
            fail('Build policy adoption template changed')
    if MARKER not in _vault_path(service.vault, target['path']).read_text(encoding='utf-8'):
        fail('adopted Build template lacks program acceptance')
    reports = []
    for name in ('previous', 'new'):
        pause = report[name+'_pause']
        raw = _vault_path(service.vault, pause['report_ref']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != report[name+'_pause_sha256']:
            fail('Build policy adoption pause report changed')
        saved = _load_json(_vault_path(service.vault, pause['report_ref']))
        if saved['evidence_digest'] != pause['evidence_digest'] or 'sha256:'+fingerprint(saved['evidence']) != pause['evidence_digest']:
            fail('Build policy adoption pause digest changed')
        reports.append(saved)
    refresh = report.get('batch_metadata_refresh')
    if refresh:
        _verify_batch_refresh(service.vault, refresh, reports[0]['evidence'])
    if (request['evidence_digest'] != report['previous_pause']['evidence_digest']
            or rebase_pause_evidence(reports[0]['evidence'], old, target, refresh) != reports[1]['evidence']):
        fail('Build policy adoption changed domain evidence')


def _fail(message):
    raise ContractError('REVIEW_REQUIRED', '$.reviews', message)


def _lines(text):
    # Ignore Markdown decoration/whitespace, not words, numbers or table cells.
    return Counter(line for raw in text.splitlines()
                   if (line := re.sub(r'\s+', '', re.sub(r'^(?:#{1,6}\s+|>\s*|[-*+]\s+)', '',
                                                        raw.strip()).replace('**', ''))))


def _revised(service, record, candidate):
    predecessor = record.get('supersedes_pass_id')
    if not predecessor:
        return False
    root = _vault_path(service.vault, f"_system/knowledge-builds/task-{record['task_id']}/passes")
    prior = next((p for path in root.glob('*.json') if (p := _load_json(path))['pass_id'] == predecessor), None)
    if prior is None or fingerprint({k: v for k, v in prior.items() if k != 'pass_id'}) != predecessor:
        _fail('citation predecessor is absent or changed')
    old = next((c for c in prior['candidates'] if c['candidate_id'] == candidate['candidate_id']), None)
    # Version identity alone does not force every sibling candidate to be reviewed.
    return old != candidate or prior['inspections'] != record['inspections']


def assess(service, run, page, *, selected=None, parent_content=None, committed_review=None):
    """Pin current page evidence and name concrete conservative review triggers.

    QA is retained by the page contract, not converted into another model call.
    Missing/stale evidence is an error, never a semantic-review trigger.
    """
    decision = next(d for d in run['decisions'] if d['page_id'] == page['page_id'])
    if selected is None:
        candidates, _ = service._pass_index([t['task_id'] for t in run['tasks']])
        selected = [candidates.get((r['pass_id'], r['candidate_id'])) for r in decision['candidate_refs']]
    if not selected or any(pair is None or pair[0]['pass_kind'] != 'citation' for pair in selected):
        _fail('current terminal citation evidence is required')
    for record, _ in selected:
        validate_record('knowledge_pass', record)
        if fingerprint({k: v for k, v in record.items() if k != 'pass_id'}) != record['pass_id']:
            _fail('citation fingerprint changed')
    parent_sha = None
    if page['parent_revision_id']:
        prior = _load_json(_vault_path(service.vault,
            f"_system/knowledge-builds/page-revisions/{page['page_id']}/{page['parent_revision_id']}.json"))
        validate_record('page_revision', prior)
        parent_sha = prior['authored_sha256']
        if parent_content is None and committed_review is None:
            subjects = service.list_identities()['subjects']
            subject = next(s for s in subjects if s['subject_id'] == page['subject_id'])
            raw = _vault_path(service.vault, subject['current_path']).read_bytes()
            if _sha(raw) != parent_sha:
                _fail('current parent bytes differ from the pinned parent revision')
            parent_content = raw.decode('utf-8')
        elif parent_content is not None and _sha(parent_content.encode('utf-8')) != parent_sha:
            _fail('reviewed parent differs from the pinned parent revision')
    from .finalize_revision_state import content_path
    content = content_path(service,run,page).read_bytes()
    if _sha(content) != page['authored_sha256']:
        _fail('draft changed before acceptance')
    resources = {r['unit_ref']['resource_id'] for _, c in selected for r in c['support_refs']}
    scopes = {c['applicability'].strip() for _, c in selected if c['applicability'].strip()}
    variants = {}
    for _, candidate in selected:
        variants.setdefault((candidate['kind'], candidate['name'].casefold()), set()).add(
            fingerprint({k: candidate[k] for k in ('finding', 'applicability', 'conditions', 'exceptions')}))
    risks = []
    if len(resources) > 1:
        risks.append('cross_source_merge')
    if len(scopes) > 1:
        risks.append('different_applicability')
    if any(len(v) > 1 for v in variants.values()):
        risks.append('claim_variants')
    if any(_revised(service, r, c) for r, c in selected):
        risks.append('revised_citation')
    if decision['identity']['kind'] == 'analysis':
        risks.append('synthesized_analysis')
    if parent_content is not None and _lines(parent_content) - _lines(content.decode('utf-8')):
        risks.append('parent_content_rewritten')
    elif committed_review and 'parent_content_rewritten' in committed_review['risk_reasons']:
        risks.append('parent_content_rewritten')
    evidence = {'policy': POLICY, 'page_id': page['page_id'], 'authored_sha256': page['authored_sha256'],
                'parent_revision_id': page['parent_revision_id'], 'parent_authored_sha256': parent_sha,
                'decision': decision, 'citations': [{'pass': r, 'candidate': c} for r, c in selected],
                'qa_status': page['qa_status'], 'business_status': page['business_status'],
                'visibility': page['visibility']}
    return {'policy': POLICY, 'risk_reasons': risks, 'evidence_sha256': fingerprint(evidence)}


def record(assessment, *, review_audit_ref=None):
    return {'contract': CONTRACT, **assessment,
            'independent_review': 'approved' if review_audit_ref else 'not_performed',
            'review_audit_ref': review_audit_ref}


def validate_acceptance(service, run, page, review):
    """Recompute risk/evidence under the ordinary Finalize lock before writes."""
    acceptance = review.get('acceptance')
    if acceptance is None:
        return  # Legacy/manual reviews keep their original explicit review contract.
    validate_record('build_acceptance', acceptance)
    from .finalize_revision_state import active_for_run
    local=active_for_run(service,run).get(page['page_id'])
    if local and local.get('grants'):
        reviewed=local.get('review_audit',{})
        if (local.get('state')!='revision_reviewed' or local.get('review_result',{}).get('decision')!='approved'
                or acceptance['independent_review']!='approved'
                or acceptance['review_audit_ref']!=reviewed.get('ref')):
            _fail('controlled local revision requires its actual independent native approval')
    # Sidecars are written before page replacement. Exact committed metadata can
    # finish a partially persisted run without needing the overwritten parent.
    committed_review = None
    sidecar = _vault_path(service.vault,
        f"_system/knowledge-builds/page-revisions/{page['page_id']}/{page['revision_id']}.json")
    if sidecar.is_file():
        saved = _load_json(sidecar)
        expected_page = {**page, 'state': 'committed',
                         'review': {k: review[k] for k in ('actor', 'note', 'acceptance')}}
        if saved != expected_page:
            _fail('partial committed acceptance differs from this request')
        committed_review = acceptance
    expected = assess(service, run, page, committed_review=committed_review)
    if any(acceptance[k] != v for k, v in expected.items()):
        _fail('program acceptance risk or evidence changed')
    ref = acceptance['review_audit_ref']
    if acceptance['independent_review'] == 'not_performed':
        if ref is not None or expected['risk_reasons']:
            _fail('a triggered review cannot be replaced with program acceptance')
    else:
        if not ref:
            _fail('independent approval requires its durable native audit')
        audit = _load_json(_vault_path(service.vault, ref))
        model = audit.get('model', {})
        prefix = f"_system/ledgers/ingest-workflows/{audit.get('workflow_id')}/semantic-calls/"
        if (audit.get('contract') != 'hermes-fixed-semantic-call/v1' or audit.get('node') != 'build-finalize'
                or not ref.startswith(prefix) or '/' in ref[len(prefix):]
                or audit.get('phase') != 'page-review' or model.get('origin') != 'hermes_native_model'
                or not audit.get('response_sha256') or model.get('response_sha256') != audit['response_sha256']):
            _fail('independent approval lacks native page-review provenance')
