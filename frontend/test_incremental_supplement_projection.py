"""Linked supplements use the original console entry and its normal pipeline."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from incremental import command, fingerprint, progress, root_for
from incremental_map_preview import CONFIRM
from incremental_projection import STAGES


SOURCE = '10_Raw/one.pdf'
OTHER = '10_Raw/two.pdf'
SOURCE_SHA = 'a' * 64
PACKET = 'b' * 64


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value).encode())


@pytest.fixture
def supplement(tmp_path):
    directory = tmp_path / 'console/parent'
    request = {'operation_id': 'parent', 'actor': 'operator', 'vault_name': 'Fixture',
               'vault_path': str(tmp_path / 'vault'), 'sources': [SOURCE, OTHER],
               'retrieval_policy': {'query_enabled': False}}
    root = root_for(request)
    source = {'path': SOURCE, 'sha256': SOURCE_SHA}
    other = {'path': OTHER, 'sha256': 'c' * 64}
    parent = {'run_id': 'parent', 'actor': 'operator', 'state': 'partial', 'revision': 8,
              'source_manifest': [source, other], 'round_ids': ['round-1'],
              'sources': {SOURCE: {'state': 'mapping_held', 'snapshot_id': 'map-snapshot', 'reason': 'original held'},
                          OTHER: {'state': 'failed'}}}
    control = {'state': 'active', 'revision': 4, 'actor': 'operator', 'active_scopes': {}}
    authorized = {'run_id': 'parent', 'actor': 'operator', 'operation_id': 'operator-map',
                  'run_revision': 8, 'control_revision': 4, 'source_path': SOURCE, 'source_sha256': SOURCE_SHA}
    child_id = 'map-supplement-' + fingerprint(['parent', SOURCE, PACKET])[:48]
    child = root.parent / child_id
    journal = root.parent.parent / 'incremental-map-recoveries/parent/operator-map.json'
    lineage = child / 'map-supplement.json'
    put(lineage, {'contract': 'incremental-map-supplement/v1', 'supplement_run_id': child_id,
                  'request': {**authorized, 'request_ids': [PACKET]}, 'completed': [{'request_id': 'past-packet'}]})
    pointer = {'ref': lineage.relative_to(root.parents[3]).as_posix(),
               'sha256': hashlib.sha256(lineage.read_bytes()).hexdigest()}
    run = {'run_id': child_id, 'actor': 'operator', 'state': 'active', 'revision': 3,
           'sources': {SOURCE: {'state': 'prepared', 'snapshot_id': 'map-snapshot'}},
           'source_manifest': [source], 'map_supplement': pointer, 'round_ids': [], 'map_result_ids': [],
           'consumed_map_ids': []}
    head = {'state': 'active', 'revision': 2, 'actor': 'operator', 'active_scopes': {}}
    for path, value in ((directory / 'request.json', request), (root / 'run.json', parent),
                        (root / 'control/head.json', control), (child / 'run.json', run), (child / 'control/head.json', head)):
        put(path, value)
    put(journal, {'contract': 'incremental-map-operator-recovery/v1', 'state': 'prepared',
                  'request': {**authorized, 'strategy': 'supplemental', 'request_id': PACKET}})
    launcher = {'id': 'confirmation', 'action': CONFIRM, 'strategy': 'supplemental', 'preview_key': 'operator-map'}
    skills = tmp_path / 'skills'
    put(skills / 'hermes-obsidian-governed-ingest-orchestrator/scripts/manage_incremental_ingest.py', {})
    return SimpleNamespace(directory=directory, request=request, root=root, child_id=child_id, child=child, run=run,
                           head=head, launcher=launcher, config={'skills_root': str(skills)}, lineage=lineage)


def view(value, *, attempts=(), active=False, details=True):
    return progress(value.directory, value.request, attempts, active, False, details)


def source_phases(state):
    source, other = state['incremental']['sources']
    assert other['display_state'] == 'failed'
    assert [phase['key'] for phase in source['pipeline']] == [key for key, _ in STAGES]
    return source, {phase['key']: phase for phase in source['pipeline']}


def mapped(value):
    value.run.update(round_ids=['round-1'], active_round_id='round-1', map_result_ids=['map-child'], revision=7)
    value.run['sources'][SOURCE].update(state='mapped', map_result_id='map-child')
    put(value.child / 'run.json', value.run)
    put(value.child / 'rounds/round-1/input.json', {'snapshots': ['map-snapshot'], 'map_result_ids': ['map-child']})


def test_live_and_closed_supplement_uses_the_same_normal_file_and_run_stages(supplement):
    initial = view(supplement, attempts=[supplement.launcher], active=True)
    assert view(supplement, attempts=[supplement.launcher], active=True, details=False)['state'] == 'running'
    assert initial['incremental']['original_state'] == 'partial'
    source, phases = source_phases(initial)
    assert source['supplement_run_id'] == supplement.child_id and source['display_state'] == 'running'
    assert phases['mapping']['state'] == 'running'
    result, = phases['mapping']['map_recovery']['results']
    assert result['reused_receipts'] == 1 and result['supplement_run_id'] == supplement.child_id
    assert not [action for action in phases['mapping']['actions'] if action['id'] == CONFIRM]

    mapped(supplement)
    supplement.head['active_scopes'] = {'writer': {'scope_kind': 'round', 'scope_id': 'round-1', 'stage': 'writing'}}
    put(supplement.child / 'control/head.json', supplement.head)
    put(supplement.child / 'rounds/round-1/page-changes.json', [
        {'action': 'create', 'change_id': 'change', 'page_id': 'page', 'coverage_scope': {'snapshots': ['map-snapshot']}}])
    writing = view(supplement, attempts=[supplement.launcher], active=True)
    _, phases = source_phases(writing)
    assert [phases[key]['state'] for key in ('mapping', 'coordination', 'writing')] == ['completed', 'completed', 'running']
    assert phases['writing']['recovery_scopes'][0]['page_count'] == 1
    original_round, child_round = writing['incremental']['rounds']
    assert original_round['round_id'] == child_round['round_id'] == 'round-1'
    assert 'supplement_run_id' not in original_round and child_round['supplement_run_id'] == supplement.child_id

    supplement.run.update(state='completed', active_round_id=None, consumed_map_ids=['map-child'])
    put(supplement.child / 'run.json', supplement.run)
    put(supplement.child / 'rounds/round-1/writing-disposition.json', {'written': [{}], 'held': []})
    put(supplement.child / 'rounds/round-1/delivery.json', {'state': 'committed', 'release_id': 'release-new'})
    terminal = view(supplement)
    _, phases = source_phases(terminal)
    assert all(phases[key]['state'] == 'completed' for key, _ in STAGES)
    assert terminal['state'] == terminal['incremental']['supplemental_run']['state'] == 'completed'
    assert terminal['incremental']['original_state'] == 'partial'


def test_child_network_continue_uses_child_revisions_and_round(supplement):
    mapped(supplement)
    boundary = {'stage': 'network_recovery', 'scope': {'scope_kind': 'round', 'scope_id': 'round-1'},
                'evidence': {'phase': 'writing'}}
    put(supplement.child / 'control/head.json', {**supplement.head, 'state': 'paused', 'boundary': boundary})
    state = view(supplement)
    _, phases = source_phases(state)
    assert state['state'] == phases['writing']['state'] == 'paused'
    action, = state['nodes'][0]['actions']
    assert action['id'] == 'incremental-continue-round' and action['supplement_run_id'] == supplement.child_id
    attempt = {**action, 'id': 'continue', 'action': action['id']}
    argv = command(supplement.directory, attempt, supplement.config, put)
    operation = json.loads(Path(argv[-1]).read_bytes())
    assert operation['run_id'] == supplement.child_id and operation['expected_revision'] == 2
    assert operation['network_recovery'] == {'round_id': 'round-1', 'run_revision': 7}
    with pytest.raises(ValueError):
        command(supplement.directory, {**attempt, 'supplement_run_id': 'other-child'}, supplement.config, put)


def test_child_closure_action_stays_bound_to_its_own_review(supplement):
    mapped(supplement)
    supplement.run.update(active_round_id=None, consumed_map_ids=['map-child'],
                          reservations={'unverified': {'phase': 'writing', 'state': 'reserved'}})
    put(supplement.child / 'run.json', supplement.run)
    put(supplement.child / 'rounds/round-1/settlement.json', {'state': 'held'})
    boundary = {'stage': 'closure', 'scope': {'scope_kind': 'run', 'scope_id': supplement.child_id}}
    put(supplement.child / 'control/head.json', {**supplement.head, 'state': 'paused', 'boundary': boundary})
    review, = view(supplement)['nodes'][-1]['actions']
    assert review['id'] == 'incremental-closure-preview' and review['supplement_run_id'] == supplement.child_id
    argv = command(supplement.directory, {**review, 'id': 'review', 'action': review['id']}, supplement.config, put)
    operation = json.loads(Path(argv[-2]).read_bytes())
    assert operation['run_id'] == supplement.child_id and argv[-1] == '--preview'
    assert operation['boundary_fingerprint'] == fingerprint(boundary)

    done = {**review, 'id': 'review', 'action': review['id'], 'returncode': 0}
    report = supplement.directory / 'attempts/review/closure-preview.json'
    put(report, {'request': {**operation, 'run_id': 'parent'}, 'report_sha256': 'review', 'reservations': {}})
    assert len(view(supplement, attempts=[done])['nodes'][-1]['actions']) == 1
    put(report, {'request': operation, 'report_sha256': 'review', 'reservations': {}})
    review, confirm = view(supplement, attempts=[done])['nodes'][-1]['actions']
    argv = command(supplement.directory, {**confirm, 'id': 'close', 'action': confirm['id']}, supplement.config, put)
    saved = json.loads(Path(argv[-1]).read_bytes())
    assert saved == {**operation, 'expected_report_sha256': 'review'}


def test_changed_child_lineage_does_not_project_an_unbound_run(supplement):
    put(supplement.lineage, {'contract': 'incremental-map-supplement/v1'})
    state = view(supplement, active=True, attempts=[supplement.launcher])
    source, phases = source_phases(state)
    assert state['state'] == 'partial'
    assert 'supplement_run_id' not in source and phases['mapping']['state'] == 'held'
