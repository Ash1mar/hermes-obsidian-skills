"""An ordinary round can publish a complete section from its saved native replies."""

from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_incremental_pages import decision, publish_sources, vault


SKILL = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator'
sys.path[:0] = [str(SKILL / 'scripts'), str(SKILL / 'lib')]

from hermes_source_units import ContractError, FileVaultFinalizeService, short_build
from hermes_source_units.incremental_control import IncrementalControl
from hermes_source_units.incremental_pages import AUTHORIZATION, normalize_page_changes
from hermes_source_units.incremental_sources import pinned, read_pin, snapshot_content
from hermes_source_units.page_governance import POLICY
from hermes_source_units.source_units import _json_bytes, _load_json, _sha, _write_atomic
from hermes_source_units.validation import fingerprint
import incremental_native
import incremental_recovery
import incremental_runtime
import incremental_writing
from incremental_writing_evidence import EvidenceCatalogue
import prepare_short_evidence_test
import run_short_evidence_test as native


def test_ordinary_writing_publishes_only_the_complete_section_and_reuses_it_after_restart(vault, monkeypatch):
    save = prepare_short_evidence_test.save
    source, = publish_sources(vault, ['\n\n'.join([
        '# Operating requirements',
        '## 4.1 Inlet isolation',
        'The inlet valve shall remain fully open during service. ' * 13,
        '## 4.2 Outlet pressure',
        'The required outlet pressure shall comply with Table 4.2.1. ' * 13,
    ]) + '\n'])
    finalize = FileVaultFinalizeService(vault)
    build = finalize.build
    registry_path = vault / '_system/metadata/document-registry.json'

    def update_registry(revision, *, original_status='active', add_source=False):
        registry = _load_json(registry_path)
        registry['registry_revision'] = revision
        registry['records'][0]['governance_status'] = original_status
        if add_source:
            registry['records'].append({**registry['records'][0], 'document_id': 'unrelated-document',
                                        'version_id': 'unrelated-version', 'resource_id': 'unrelated-resource'})
        _write_atomic(registry_path, _json_bytes(registry))

    update_registry(5)
    ref = source['unit_ref']
    _, units, _ = build.source._load_set(ref['resource_id'], ref['unit_set_id'])
    original_file = vault / '10_Raw/source-1.md'
    original_bytes = original_file.read_bytes()
    original = {'path': original_file.relative_to(vault).as_posix(), 'sha256': _sha(original_bytes)}
    source_outcome = {'status': 'ready', 'path': original['path'], 'content_sha256': original['sha256'],
                      'resource_id': ref['resource_id'], 'unit_set_id': ref['unit_set_id'], 'artifact_refs': []}
    snapshot, _ = snapshot_content(vault, 'agent', original, source_outcome)
    run_id, round_id, snap = 'run-fixture', 'round-1', snapshot['snapshot_id']
    run_root = vault / '_system/ledgers/incremental-ingest' / run_id
    round_root = run_root / 'rounds' / round_id
    native_root = run_root / 'native/round' / round_id
    writer = native_root / 'writing'
    scope = {'scope_kind': 'round', 'scope_id': round_id}
    frozen = {'run_id': run_id, 'round_id': round_id, 'snapshots': [snap, 'unrelated-snapshot'],
              'map_result_ids': ['map-fixture'], 'context_map_result_ids': ['unrelated-map']}
    frozen['input_fingerprint'] = fingerprint(frozen)
    save(round_root / 'input.json', frozen)
    map_body = round_root / 'mapped.json'
    save(map_body, {'topics': {}})
    mapped = {'snapshot_id': snap, 'map_result_id': 'map-fixture', 'alias_table': {
        f'E{index}': {'unit_ref': unit['ref'], 'snapshot_id': snap, 'sha256': unit['content_sha256']}
        for index, unit in enumerate(units, 1)}, 'coverage': [
            {'unit_ref': unit['ref'], 'disposition': 'mapped'} for unit in units],
        'results': [pinned(vault, map_body.relative_to(vault).as_posix())]}
    hydrated = [{**mapped, 'mapped': _load_json(map_body)}]
    catalogue = EvidenceCatalogue(build, hydrated, 'agent')
    refs = {key: row['ref'] for key, row in catalogue.aliases.items() if catalogue.writing_body(row)}
    intent = decision('create', 'operating-requirements', '30_Cards/Operations/Operating requirements.md')
    intent.update(name='Operating requirements', support_aliases=list(refs))
    parent, = normalize_page_changes(build, frozen, {'changes': [intent]}, refs)
    save(round_root / 'page-changes.json', [parent])
    run = {'run_id': run_id, 'actor': 'agent', 'state': 'active', 'active_round_id': round_id, 'revision': 2,
           'reservations': {}}
    save(run_root / 'run.json', run)
    checked_revisions = []
    checked_unrelated = []

    def read_frozen_map(_, map_result_id):
        if map_result_id == 'unrelated-map':
            checked_unrelated.append(map_result_id)
            raise ContractError('ACCESS_DENIED', '$', 'unrelated context Map lost governance')
        assert map_result_id == mapped['map_result_id']
        verified, revision = snapshot_content(vault, 'agent', original, source_outcome)
        assert verified == snapshot
        checked_revisions.append(revision)
        return mapped

    domain = SimpleNamespace(vault=vault, root=lambda _: run_root, status=lambda _: run,
                             round_input=lambda *_: frozen, read_map=read_frozen_map)
    control = IncrementalControl(vault, run_id)
    control.initialize('agent', [])

    prepare = incremental_writing.prepare_work
    whole = prepare(build, catalogue, parent)
    assert len(whole['fragments']) == 1
    input_cap = whole['whole_cost']['input_size'] - 1
    work = prepare(build, catalogue, parent, input_cap=input_cap)
    assert len(work['fragments']) == 2
    inlet = work['fragments'][0]['prepared']
    headings = [row for row in inlet['projection']['view']['materials'] if row['role'] == 'context']
    assert {row['text'].strip() for row in headings} == {
        '# Operating requirements', '## 4.1 Inlet isolation'}
    assert all(row['ref'] not in inlet['view']['assigned'] and not row.get('required_dependency') for row in headings)
    monkeypatch.setattr(incremental_writing, 'prepare_work',
                        lambda *args, **options: prepare(*args, **{'input_cap': input_cap, **options}))

    operation = {'workflow_mode': 'incremental_v1', 'vault': str(vault), 'actor': 'agent',
                 'contract': 'fixture-native', 'board': 'fixture-writing', 'page_capacity': 2,
                 'incremental': {'run_id': run_id, **scope, 'stage': 'writing',
                                 'input_fingerprint': frozen['input_fingerprint']},
                 'origin': {'workflow_id': 'fixture-source', 'snapshots': frozen['snapshots']}}
    save(writer / 'operation.json', operation)
    plan_path = writer / 'fragment-plans' / (parent['change_id'] + '.json')
    save(plan_path, work)
    initial_calls = []
    for position, fragment in enumerate(work['fragments'], 1):
        row = fragment['request']
        request_id = row['id']
        prepared = fragment['prepared']
        statement = 'Leave the inlet valve fully open during service' if position == 1 else 'Retain the pressure requirement'
        body = ' '.join(f'{statement} [{handle}].' for handle in prepared['view']['assigned'])
        unresolved = [] if position == 1 else ['Required Table 4.2.1 was missing; outlet pressure cannot be determined.']
        answer = {'content': body, 'inspected': sorted(prepared['material_aliases']), 'unresolved': unresolved}
        job = writer / 'jobs' / (request_id + '.json')
        save(job, {'request': row})
        save(job.with_suffix('.result.json'), {'state': 'settled', 'task_id': f'task-{position}', 'run_id': position})
        save(job.with_suffix('.attempt-1.json'), {'request': row})
        save(writer / 'receipts' / (request_id + '.json'), {'answer': answer})
        initial_calls.append(request_id)
    original_evidence = {path: _sha(path.read_bytes()) for path in writer.rglob('*.json')}

    def verified_result(root, row):
        assert root == writer
        job = root / 'jobs' / (row['id'] + '.json')
        assert _load_json(job)['request'] == row
        result_path = job.with_suffix('.result.json')
        result = _load_json(result_path)
        return result, {'board': operation['board'], 'task_id': result['task_id'], 'run_id': result['run_id'],
                        'request': pinned(vault, job.relative_to(vault).as_posix()),
                        'result': pinned(vault, result_path.relative_to(vault).as_posix())}

    def reuse_saved_writer(_domain, _run_id, scope_kind, scope_id, phase, snapshots, **_):
        assert (scope_kind, scope_id, phase, snapshots) == ('round', round_id, 'writing', frozen['snapshots'])
        return writer, operation

    monkeypatch.setattr(incremental_runtime, 'initialize_scope', reuse_saved_writer)
    for module in (incremental_native, incremental_recovery, incremental_runtime):
        monkeypatch.setattr(module, 'verified_result', verified_result)
    monkeypatch.setattr(incremental_runtime, 'settle_usage', lambda *_: None)
    monkeypatch.setattr(native, 'stage', lambda *_: pytest.fail('saved writing must not dispatch another native job'))
    monkeypatch.setattr(incremental_native, 'service', lambda *_: domain)
    execute = incremental_runtime.execute_rows

    def execute_with_parallel_registration(*args):
        result = execute(*args)
        if build._document_registry_revision() == 5:
            update_registry(7, add_source=True)
        return result

    monkeypatch.setattr(incremental_runtime, 'execute_rows', execute_with_parallel_registration)
    disposition_path = round_root / 'writing-disposition.json'
    interrupted = []

    def interrupt_final_save(path, value):
        if path == disposition_path:
            interrupted.append(value)
            raise OSError('isolated exit before final writing receipt')
        return save(path, value)

    with monkeypatch.context() as interruption:
        interruption.setattr(prepare_short_evidence_test, 'save', interrupt_final_save)
        with pytest.raises(OSError, match='before final writing receipt'):
            incremental_writing.writing_disposition(domain, control, scope, frozen, [parent], build, hydrated)
    assert len(interrupted) == 1 and not disposition_path.exists()
    restarted = IncrementalControl(vault, run_id)
    _, disposition = incremental_writing.writing_disposition(domain, restarted, scope, frozen, [parent], build, hydrated)
    assert disposition == interrupted[0]
    assert checked_revisions == [7, 7]
    assert checked_unrelated == ['unrelated-map', 'unrelated-map']
    assert len(disposition['written']) == 1, disposition['section_holds']
    written, = disposition['written']
    held, = disposition['held']
    assert held['change_id'] == parent['change_id'] and held['page_id'] == parent['page_id']
    assert 'unresolved material' in held['reason']
    assert disposition['original_pages'] == 0
    blocked, = disposition['section_holds'][parent['change_id']]
    assert catalogue.sections[snap][blocked['section']]['title'] == '4.2 Outlet pressure'
    assert 'required fragment is incomplete' in blocked['reason']
    prepare_section = read_pin(vault, written['preparation'])
    child = prepare_section['change']
    assert child['page_id'] != parent['page_id'] and child['name'] == '4.1 Inlet isolation'
    assert child['independent_section']['parent_plan_id'] == parent['change_id']
    assert child['added_support_refs'] == inlet['support_refs']
    assert [row['request_id'] for row in prepare_section['proofs']] == initial_calls[:1]
    qualification_bytes = (vault / prepare_section['qualification']['ref']).read_bytes()
    assert all(_sha(path.read_bytes()) == digest for path, digest in original_evidence.items())
    assert {path.name for path in writer.rglob('*.attempt-1.json')} == {
        request_id + '.attempt-1.json' for request_id in initial_calls}

    formal = native_root / 'formal-build'
    formal_op = {**operation, 'board': 'fixture-formal', 'incremental': {**operation['incremental'], 'stage': 'formal-build'}}
    save(formal / 'operation.json', formal_op)
    authorization = {'contract': AUTHORIZATION, 'run_id': run_id, 'round_id': round_id, 'actor': 'agent',
                     'pages': [child['change_id']], 'page_changes': [child], 'base_release': None, 'publish': True,
                     'policy': POLICY, 'input_fingerprint': frozen['input_fingerprint'], 'source_snapshots': frozen['snapshots'],
                     'map_result_ids': frozen['map_result_ids']}
    view = {'authorization': authorization, 'written': disposition['written'], 'excluded': disposition['held'],
            'delivery_id': 'delivery-fixture', 'provider_config': 'unused-in-isolated-test'}
    row = {'id': fingerprint(view), 'phase': 'incremental-formal-build', 'view': view}
    job = formal / 'jobs' / (row['id'] + '.json')
    save(job, {'request': row})
    own = lambda: {'board': formal_op['board'], 'task_id': 'build-native', 'run_id': 1}
    monkeypatch.setattr(short_build, 'native_authority', lambda *_: {**own(), 'job_sha256': _sha(job.read_bytes())})
    index_calls = []

    def synchronize(_vault, _native, release_id, release_hash, *_):
        index_calls.append(release_id)
        return {'status': 'ready', 'release_id': release_id, 'release_hash': release_hash,
                'index_generation': 'fixture-generation'}

    monkeypatch.setattr(incremental_native, 'synchronize_round_index', synchronize)
    update_registry(8, original_status='revoked')
    with pytest.raises(ValueError, match='differs from its verified original scope and receipts'):
        incremental_native.build(formal, job, row, own, native)
    update_registry(9)
    try:
        original_file.write_bytes(original_bytes + b'Unregistered change.\n')
        with pytest.raises(ValueError, match='differs from its verified original scope and receipts'):
            incremental_native.build(formal, job, row, own, native)
    finally:
        original_file.write_bytes(original_bytes)
    proof_path = writer / 'receipts' / (initial_calls[0] + '.json')
    original_proof = proof_path.read_bytes()
    proof = _load_json(proof_path)
    proof['answer']['content'] = 'Unverified replacement [m1].'
    try:
        _write_atomic(proof_path, _json_bytes(proof))
        with pytest.raises(ValueError, match='differs from its verified original scope and receipts'):
            incremental_native.build(formal, job, row, own, native)
    finally:
        _write_atomic(proof_path, original_proof)
    assert not index_calls and finalize.status()['current_release_id'] is None
    omitted_parent = {**view, 'excluded': []}
    invalid_row = {**row, 'id': fingerprint(omitted_parent), 'view': omitted_parent}
    invalid_job = formal / 'jobs' / (invalid_row['id'] + '.json')
    save(invalid_job, {'request': invalid_row})
    with pytest.raises(ValueError, match='lost its round writing disposition'):
        incremental_native.build(formal, invalid_job, invalid_row, own, native)
    delivered = incremental_native.build(formal, job, row, own, native)
    assert checked_revisions[-1] == 9
    assert checked_unrelated[-1] == 'unrelated-map'
    assert (vault / prepare_section['qualification']['ref']).read_bytes() == qualification_bytes
    assert all(_sha(path.read_bytes()) == digest for path, digest in original_evidence.items())
    release_id = delivered['delivery']['release_id']
    manifest = finalize._manifest_path(release_id)
    published = _load_json(manifest)
    assert delivered['delivery']['state'] == 'committed' and finalize.validate(release_id)['ok']
    assert [page['page_id'] for page in published['subjects']] == [child['page_id']]
    assert [page['page_id'] for page in published['delivery']['excluded']] == [parent['page_id']]
    assert [entry['id'] for entry in published['index_eligibility'] if entry['eligible']] == [child['page_id']]
    assert not (vault / parent['path']).exists() and (vault / child['path']).is_file()

    recovery = {**view, 'recovery': {'operation_id': 'reattach-fixture', 'disposition': 'continue', 'stop_after': None,
                                    'original_job': pinned(vault, job.relative_to(vault).as_posix())}}
    resumed_row = {**row, 'id': fingerprint(recovery), 'view': recovery}
    resumed_job = formal / 'jobs' / (resumed_row['id'] + '.json')
    save(resumed_job, {'request': resumed_row})
    revision_before, manifest_before = finalize.status()['revision'], manifest.read_bytes()
    recovered = incremental_native.build(formal, resumed_job, resumed_row, own, native)
    assert recovered == delivered and index_calls == [release_id]
    assert finalize.status()['revision'] == revision_before and manifest.read_bytes() == manifest_before


@pytest.mark.parametrize('advances', [1, 2])
def test_ordinary_section_review_retries_document_registry_advance_once(monkeypatch, advances):
    from incremental_sections import current_ordinary_sections

    revision, observed = 5, []
    builder = SimpleNamespace(_document_registry_revision=lambda: revision,
                              source=SimpleNamespace(reading_snapshot=lambda **_: nullcontext()))
    monkeypatch.setattr(incremental_writing, 'catalogue_for_round', lambda *_, **_options:
                        SimpleNamespace(access={'registry_revision': revision}))

    def evaluate(catalogue, _unavailable):
        nonlocal revision
        observed.append(catalogue.access['registry_revision'])
        if len(observed) <= advances:
            revision += 2
        return observed[-1]

    if advances == 1:
        assert current_ordinary_sections(None, builder, {'run_id': 'same'}, evaluate) == 7
    else:
        with pytest.raises(ContractError, match='document registry advanced during section review'):
            current_ordinary_sections(None, builder, {'run_id': 'same'}, evaluate)
    assert observed == [5, 7]


def test_registry_advance_cannot_retry_an_unrelated_source_failure(monkeypatch):
    from incremental_sections import current_ordinary_sections

    revision, observed = 5, []
    builder = SimpleNamespace(_document_registry_revision=lambda: revision,
                              source=SimpleNamespace(reading_snapshot=lambda **_: nullcontext()))
    monkeypatch.setattr(incremental_writing, 'catalogue_for_round', lambda *_, **_options:
                        SimpleNamespace(access={'registry_revision': revision}))

    def evaluate(catalogue, _unavailable):
        nonlocal revision
        observed.append(catalogue.access['registry_revision'])
        revision += 2
        raise ContractError('SOURCE_CHANGED', '$', 'registered artifact bytes changed')

    with pytest.raises(ContractError, match='registered artifact bytes changed'):
        current_ordinary_sections(None, builder, {'run_id': 'same'}, evaluate)
    assert observed == [5]
