"""Map recovery preview and child continuation preserve their exact operator scope."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import server
from incremental import command, fingerprint, progress, root_for
from incremental_map_preview import CONFIRM, CONTINUE, PREVIEW, preview_path, recovery_request
from workflow import save


SOURCE = '10_Raw/source.pdf'
SOURCE_SHA = 'a' * 64
PACKET = 'b' * 64


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, value)


@pytest.fixture
def state(tmp_path):
    console_root = tmp_path / 'console'
    directory = console_root / 'parent'
    request = {'operation_id': 'parent', 'actor': 'operator', 'vault_name': 'Fixture',
               'vault_path': str(tmp_path / 'vault'), 'workflow_mode': 'incremental_v1',
               'sources': ['/source.pdf'], 'retrieval_policy': {'query_enabled': False}}
    root = root_for(request)
    run = {'run_id': 'parent', 'actor': 'operator', 'state': 'partial', 'revision': 8,
           'source_manifest': [{'path': SOURCE, 'sha256': SOURCE_SHA}], 'round_ids': [],
           'sources': {SOURCE: {'state': 'mapping_held', 'snapshot_id': 'snapshot'}}}
    control = {'state': 'active', 'revision': 4, 'active_scopes': {}, 'actor': 'operator'}
    config = {'state': str(console_root), 'writable': True, 'skills_root': str(tmp_path / 'skills')}
    script = Path(config['skills_root']) / 'hermes-obsidian-governed-ingest-orchestrator/scripts/manage_incremental_ingest.py'
    script.parent.mkdir(parents=True)
    script.touch()
    for path, value in ((directory / 'request.json', request),
                        (root / 'run.json', run), (root / 'control/head.json', control)):
        put(path, value)
    return SimpleNamespace(directory=directory, request=request, config=config, root=root, run=run, control=control)


def report(operation, strategy='supplemental'):
    document = {'request': operation, 'strategies': [
        {'id': name, 'allowed': name == strategy, 'reason': 'scope checked', 'request_ids': [PACKET] if name == strategy else []}
        for name in ('original', 'supplemental')
    ], 'outcomes': [{'request_id': PACKET, 'task_id': 't_map', 'disposition': 'eligible'}]}
    return {**document, 'report_sha256': fingerprint(document)}


def actions(state, *, active=False, historical=False):
    result = progress(state.directory, state.request, [], active, historical, True)
    source, = result['incremental']['sources']
    return next(row for row in source['pipeline'] if row['key'] == 'mapping')


@pytest.mark.parametrize(('run_state', 'control_state', 'active'), (
    ('partial', 'active', False), ('completed', 'active', False), ('active', 'paused', False),
    ('active', 'pausing', True), ('active', 'cancelled', False), ('active', 'active', True),
))
def test_map_preview_available_for_registered_source_in_each_state(state, run_state, control_state, active):
    state.run['state'] = run_state
    state.control['state'] = control_state
    put(state.root / 'run.json', state.run)
    put(state.root / 'control/head.json', state.control)
    options = actions(state, active=active)['actions']
    preview, = [row for row in options if row['id'] == PREVIEW]
    assert preview['source_sha256'] == SOURCE_SHA and preview['gateway_required'] is False
    assert not any(row['id'] in (CONFIRM, 'incremental-retry-source') for row in options)
    assert not actions(state, historical=True)['actions']


def test_preview_works_without_dispatch_lease_and_confirms_returned_revision(state, monkeypatch):
    console = server.Console(state.config, [])
    monkeypatch.setattr(console, 'lock', lambda: pytest.fail('preview attempted to use the dispatch lease'))
    monkeypatch.setattr(console, 'is_active', lambda *_: True)
    preview, = actions(state, active=True)['actions']
    before = (state.root / 'run.json').read_bytes()
    seen = []

    def inspect_cli(argv, **_options):
        assert argv[-4] == 'recover-map' and argv[-1] == '--preview'
        operation = json.loads(Path(argv[-2]).read_bytes())
        seen.append(operation)
        bound = {**operation, 'run_revision': operation['run_revision'] + 1}
        return SimpleNamespace(returncode=0, stdout=json.dumps(report(bound)), stderr=b'')

    monkeypatch.setattr(server.subprocess, 'run', inspect_cli)
    assert console.launch({**preview, 'action': PREVIEW}, 'parent') == {'id': 'parent', 'preview': True}
    assert (state.root / 'run.json').read_bytes() == before
    assert not (Path(state.config['state']) / 'active.json').exists()
    assert not (state.directory / 'attempts').exists()
    assert not [row for row in actions(state)['actions'] if row['id'] == CONFIRM]

    state.run['revision'] += 1
    put(state.root / 'run.json', state.run)
    confirm, = [row for row in actions(state)['actions'] if row['id'] == CONFIRM]
    assert confirm['strategy'] == 'supplemental' and confirm['preview_key'] == seen[0]['operation_id']
    assert confirm['run_revision'] == state.run['revision'] and confirm['request_ids'] == [PACKET]
    assert not [row for row in actions(state, active=True)['actions'] if row['id'] == CONFIRM]
    emitted = []
    attempt = {**confirm, 'id': 'confirmation', 'action': CONFIRM, 'request_id': PACKET}
    argv = command(state.directory, attempt, state.config, lambda _, value: emitted.append(value))
    assert argv[-3] == 'recover-map' and '--preview' not in argv
    assert emitted == [{**seen[0], 'run_revision': state.run['revision'], 'strategy': 'supplemental',
                        'request_id': PACKET, 'expected_report_sha256': confirm['report_sha256']}]
    for override in ({'report_sha256': 'f' * 64}, {'request_id': 'c' * 64}, {'strategy': 'original'},
                     {'control_revision': state.control['revision'] - 1}):
        with pytest.raises(ValueError):
            command(state.directory, {**attempt, **override}, state.config, lambda *_: pytest.fail('saved a stale request'))


def test_durable_child_mapping_stop_is_linked_to_the_original_source(state):
    operation = recovery_request(state.request, state.run, state.control, SOURCE, SOURCE_SHA, ValueError)
    put(preview_path(state.directory, operation), report(operation))
    authorized = {**operation, 'strategy': 'supplemental', 'request_id': PACKET}
    successor = 'map-supplement-child'
    child = state.root.parent / successor
    journal = state.root.parent.parent / 'incremental-map-recoveries' / 'parent' / (operation['operation_id'] + '.json')
    lineage = child / 'map-supplement.json'
    put(lineage, {'contract': 'incremental-map-supplement/v1', 'supplement_run_id': successor,
                  'request': {**operation, 'request_ids': [PACKET]}})
    pointer = {'ref': lineage.relative_to(state.root.parents[3]).as_posix(),
               'sha256': hashlib.sha256(lineage.read_bytes()).hexdigest()}
    child_run = {'run_id': successor, 'actor': 'operator', 'state': 'active', 'revision': 11,
                 'sources': {SOURCE: {'state': 'mapped', 'snapshot_id': 'snapshot', 'map_result_id': 'map-child'}},
                 'map_result_ids': ['map-child'], 'map_supplement': pointer}
    boundary = {'scope': {'scope_kind': 'source', 'scope_id': 'snapshot'}, 'stage': 'mapping',
                'evidence': {'map_result_id': 'map-child', 'map_supplement': pointer}}
    child_control = {'state': 'paused', 'revision': 6, 'actor': 'operator', 'active_scopes': {}, 'boundary': boundary}
    put(child / 'run.json', child_run)
    put(child / 'control/head.json', child_control)
    put(journal, {'contract': 'incremental-map-operator-recovery/v1', 'state': 'committed', 'request': authorized,
                  'receipt': {'strategy': 'supplemental', 'supplement_run_id': successor, 'state': 'mapping-paused',
                              'map_result_id': 'map-child', 'run_revision': 11, 'control_revision': 6,
                              'boundary_fingerprint': fingerprint(boundary)}})

    mapping = actions(state)
    linked, = mapping['map_recovery']['results']
    assert (linked['supplement_run_id'], linked['state'], linked['boundary'],
            linked['map_result_ids']) == (successor, 'paused', 'mapping', ['map-child'])
    continuation, = [row for row in mapping['actions'] if row['id'] == CONTINUE]
    attempt = {**continuation, 'id': 'continue-child', 'action': CONTINUE}
    emitted = []
    argv = command(state.directory, attempt, state.config, lambda _, value: emitted.append(value))
    assert argv[-3] == 'control'
    assert emitted[0]['run_id'] == successor and emitted[0]['expected_run_revision'] == 11
    assert emitted[0]['expected_revision'] == 6 and emitted[0]['boundary_fingerprint'] == fingerprint(boundary)
    put(child / 'control/head.json', {**child_control, 'revision': 7})
    assert not [row for row in actions(state)['actions'] if row['id'] == CONTINUE]
    with pytest.raises(ValueError):
        command(state.directory, attempt, state.config, lambda *_: pytest.fail('saved a stale child request'))
