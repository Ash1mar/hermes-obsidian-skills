"""Small, deterministic Build acceptance policy; never a semantic approval."""
from collections import Counter
import re

from .validation import ContractError
from .source_units import _load_json, _vault_path, _sha
from .validation import fingerprint, validate_record

CONTRACT = 'program-build-acceptance/v1'
POLICY = 'explicit-page-risks/v1'


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
                or not audit.get('response_sha256') or model.get('response_sha256') != audit['response_sha256']
                or audit.get('validated') is False):
            _fail('independent approval lacks native page-review provenance')
