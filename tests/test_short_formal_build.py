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


@pytest.mark.parametrize('advisory', [False, True])
@pytest.mark.parametrize('cards', [False, True])
def test_native_short_build_keeps_qa_out_of_partial_release(vault: Path, tmp_path, monkeypatch, advisory, cards):
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
    issue = {'id': 'notice', 'items': [], 'status': 'unlocated', 'writing': 'allow', 'publication': 'review'}
    candidates = [{'short_id': key, 'name': f'Page {i}', 'authored_sha256': _sha(bodies[key]), 'support_refs': [ref],
                   'native_fragments': [{'receipt_sha256': fingerprint(key)}], 'qa_issues': [issue] if i else [],
                   'qa_status': 'qa_required' if i else 'usable', 'publication_authorized': not i,
                   'source_eligible_at_admission': True}
                  for i, key in enumerate(ids[:2])]
    if cards:
        for i, candidate in enumerate(candidates):
            candidate['card_path'] = f'30_Cards/工程设计/设计卡片 {i}.md'
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
    pins = [run['short_evidence']]
    if advisory:
        job.write_bytes(_json_bytes({'request': {'phase': 'short-formal-qa',
            'view': {'authorization': grant, 'run_id': run['run_id']}}}))
        dependencies = {key: [{'materials': ['S1-B1'], 'receipt_sha256': fingerprint(key)}] for key in ids[:2]}
        pins.append(short_build.record_qa_advisory(service.build, committed, dependencies, root, job))
        assert held['qa_status'] == 'qa_required'
    plan = service.plan(request('release-short-test', [run['run_id']]))['plan']
    selected = {page['page_id'], held['page_id']} if advisory else {page['page_id']}
    assert set(plan['delivery']['included_page_ids']) == selected
    excluded = {short_build.identity(service.build, ids[2], 'Isolated')[1]}
    if not advisory:
        excluded.add(held['page_id'])
    assert {r['page_id'] for r in plan['delivery']['excluded']} == excluded
    assert plan['delivery']['inherited_coverage_gaps'] == pins
    release = apply(service, plan)['release']
    assert {p['page_id'] for p in release['subjects']} == selected
    assert service.validate(release['release_id'])['subject_count'] == len(selected)
    # An additive Build may describe an unchanged earlier page as outside its own selection.
    # That scope boundary must not retract the previously published revision.
    if cards and advisory:
        new_id = fingerprint('additive')
        grant2 = {**grant, 'pages': [new_id], 'base_release': release['release_id']}
        body2 = {new_id: b'# Additive\n\nSource evidence.\n'}
        candidate2 = {**candidates[0], 'short_id': new_id, 'name': 'Additive',
                      'authored_sha256': _sha(body2[new_id]), 'card_path': '30_Cards/工程设计/新增.md'}
        scope2 = {**scope, 'authorization': grant2, 'pages': [candidate2], 'excluded': [{
            'short_id': ids[0], 'name': 'Page 0', 'disposition': {'category': 'delivery_conditions',
            'reason': 'outside-exact-short-build-selection', 'reopen_condition': 'authorize the exact page'}}]}
        job.write_bytes(_json_bytes({'request': {'phase': 'short-formal-build', 'view': {'authorization': grant2}}}))
        added = short_build.admit(service.build, scope2, body2, root, job)
        reviews2 = [{'page_id': p['page_id'], 'authored_sha256': p['authored_sha256'], 'parent_authored_sha256': None,
                     'actor': 'agent', 'note': 'Program acceptance.', 'acceptance': record(assess(service.build, added, p))}
                    for p in added['page_revisions']]
        service.build.finalize({'run_id': added['run_id'], 'actor': 'agent', 'expected_revision': 1, 'reviews': reviews2})
        proposal = service.plan(request('release-additive-test', [added['run_id']], state=service.status()['revision']))['plan']
        assert selected <= {p['page_id'] for p in proposal['subjects']}
        assert added['short_evidence'] in proposal['delivery']['inherited_coverage_gaps']
        # A material exclusion for the same old page remains effective.
        from hermes_source_units import scoped_release
        short_scope = scoped_release.short_scope
        def material_exclusion(*args):
            selected_ids, omissions, pointers = short_scope(*args)
            omissions[page['page_id']].update(category='unresolved', reason='missing required evidence')
            return selected_ids, omissions, pointers
        with monkeypatch.context() as patch:
            patch.setattr(scoped_release, 'short_scope', material_exclusion)
            negative = service._build_plan(request('release-material-test', [added['run_id']], state=service.status()['revision']))
            assert page['page_id'] not in {p['page_id'] for p in negative['subjects']}
            assert page['page_id'] in {p['page_id'] for p in negative['delivery']['excluded']}
    if cards:
        from short_cards import write_navigation

        expected = [{'path': '30_Cards/工程设计/00_目录.md', 'state': 'ready', 'pages': len(selected)}]
        assert write_navigation(service, release, {'30_Cards/工程设计'}) == expected
        nav = vault / expected[0]['path']
        contents = nav.read_bytes()
        assert b'Page 0' in contents and (b'Page 1' in contents) is advisory
        assert write_navigation(service, release, {'30_Cards/工程设计'}) == expected
        assert nav.read_bytes() == contents
        nav.write_text('Manual index', encoding='utf-8')
        assert write_navigation(service, release, {'30_Cards/工程设计'}) == [
            {'path': expected[0]['path'], 'state': 'occupied'}]
        assert nav.read_text(encoding='utf-8') == 'Manual index'
    if cards and advisory:
        additional_release = apply(service, proposal)['release']
        assert selected <= {p['page_id'] for p in additional_release['subjects']}
        assert service.validate(additional_release['release_id'])['subject_count'] == len(selected) + 1
    proof = vault / pins[-1]['ref']
    proof.write_bytes(proof.read_bytes() + b' ')
    with pytest.raises(ContractError, match='changed'):
        service.validate(release['release_id'])


def test_only_localized_actual_material_defect_is_a_hold():
    from hermes_source_units.qa_policy import annotation_disposition

    localized = {'id': 'asset', 'status': 'localized', 'items': ['S1-A4'], 'writing': 'hold', 'publication': 'hold'}
    assert annotation_disposition(localized, ['S1-A4'])['effect'] == 'material-hold'
    assert annotation_disposition(localized, ['S1-A5'])['effect'] == 'annotation'
    assert annotation_disposition({**localized, 'status': 'unlocated'}, ['S1-A4'])['effect'] == 'annotation'
