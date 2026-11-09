"""Exercise ordinary commit and scoped publication with typed short evidence."""
from pathlib import Path

import pytest

from test_p3_knowledge_build import publish_sources, vault
from test_p4_vault_finalize import apply, request
from hermes_source_units import ContractError, FileVaultFinalizeService
from hermes_source_units import short_build
from hermes_source_units.build_acceptance import assess, record
from hermes_source_units.page_governance import POLICY
from hermes_source_units.source_units import _json_bytes, _sha
from hermes_source_units.validation import fingerprint


def test_native_short_build_keeps_qa_out_of_partial_release(vault: Path, tmp_path, monkeypatch):
    ref, = publish_sources(vault, ['# One\n\nSource evidence.\n'])
    service = FileVaultFinalizeService(vault)
    root = tmp_path / 'experiment'
    root.mkdir()
    operation = {'vault': str(vault), 'origin': {'workflow_id': 'origin'}}
    (root / 'operation.json').write_bytes(_json_bytes(operation))
    job = root / 'job.json'
    ids = [fingerprint(n) for n in ('one', 'two', 'isolated')]
    grant = {'actor': 'agent', 'pages': ids[:2], 'publish': True, 'policy': POLICY}
    job.write_bytes(_json_bytes({'request': {'phase': 'short-formal-build', 'view': {'authorization': grant}}}))
    monkeypatch.setattr(short_build, 'native_authority', lambda *_: {'task_id': 'fixture', 'run_id': 1})
    bodies = {key: f'# Page {i}\n\nEvidence.\n'.encode() for i, key in enumerate(ids[:2])}
    candidates = [{'short_id': key, 'name': f'Page {i}', 'authored_sha256': _sha(bodies[key]), 'support_refs': [ref],
                   'native_fragments': [{'receipt_sha256': fingerprint(key)}], 'qa_issues': [{'id': 'held'}] if i else [],
                   'qa_status': 'qa_required' if i else 'usable', 'publication_authorized': not i}
                  for i, key in enumerate(ids[:2])]
    scope = {'actor': 'agent', 'authorization': grant, 'origin': operation['origin'], 'coverage_complete': False,
             'operation_sha256': _sha((root / 'operation.json').read_bytes()), 'pages': candidates,
             'excluded': [{'short_id': ids[2], 'name': 'Isolated', 'disposition': {
                 'category': 'unresolved', 'reason': 'invalid native fragment', 'reopen_condition': 'new proof'}}]}
    run = short_build.admit(service.build, scope, bodies, root, job)
    reviews = [{'page_id': p['page_id'], 'authored_sha256': p['authored_sha256'], 'parent_authored_sha256': None,
                'actor': 'agent', 'note': 'Program acceptance.', 'acceptance': record(assess(service.build, run, p))}
               for p in run['page_revisions']]
    final = {'run_id': run['run_id'], 'actor': 'agent', 'expected_revision': 1, 'reviews': reviews}
    invalid = {**final, 'reviews': [{k: v for k, v in reviews[0].items() if k != 'acceptance'}, reviews[1]]}
    with pytest.raises(ContractError, match='acceptance'):
        service.build.finalize(invalid)
    committed = service.build.finalize(final)['run']
    assert short_build.admit(service.build, scope, bodies, root, job) == committed
    page, held = committed['page_revisions']
    plan = service.plan(request('release-short-test', [run['run_id']]))['plan']
    assert plan['delivery']['included_page_ids'] == [page['page_id']]
    assert {r['page_id'] for r in plan['delivery']['excluded']} == {
        held['page_id'], short_build.identity(service.build, ids[2], 'Isolated')[1]}
    assert plan['delivery']['inherited_coverage_gaps'] == [run['short_evidence']]
    release = apply(service, plan)['release']
    assert [p['page_id'] for p in release['subjects']] == [page['page_id']]
    assert service.validate(release['release_id'])['subject_count'] == 1
    proof = vault / run['short_evidence']['ref']
    proof.write_bytes(proof.read_bytes() + b' ')
    with pytest.raises(ContractError, match='changed'):
        service.validate(release['release_id'])
