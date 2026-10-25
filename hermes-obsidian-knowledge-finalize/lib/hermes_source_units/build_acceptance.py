"""Small, deterministic Build acceptance policy; never a semantic approval."""
from collections import Counter
import copy
import hashlib
import re

from .validation import ContractError
from .source_units import _load_json, _vault_path, _sha
from .validation import fingerprint, validate_record

CONTRACT = 'program-build-acceptance/v1'
POLICY = 'explicit-page-risks/v1'
ADOPTION = 'build-acceptance-policy-adoption/v1'
MARKER = '<!-- hermes-program-build-acceptance/v1 -->'


def rebase_pause_evidence(evidence, previous_pin, target_pin):
    """Change exactly one template dependency; all domain evidence stays pinned."""
    updated = copy.deepcopy(evidence)
    if updated['files'].pop(previous_pin['path'], None) != previous_pin['template_hash'][7:]:
        raise ContractError('STALE_INPUT', '$', 'old pause does not pin the previous Build template')
    if target_pin['path'] in updated['files']:
        raise ContractError('STALE_INPUT', '$', 'new Build template already occurs in old pause evidence')
    updated['files'][target_pin['path']] = target_pin['template_hash'][7:]
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
    if (request['evidence_digest'] != report['previous_pause']['evidence_digest']
            or rebase_pause_evidence(reports[0]['evidence'], old, target) != reports[1]['evidence']):
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
    content = _vault_path(service.vault,
        f"_system/knowledge-builds/{run['run_id']}/pages/{page['page_id']}.md").read_bytes()
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
