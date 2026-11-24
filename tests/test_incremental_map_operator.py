"""Operator Map recovery keeps review read-only and confirmation bound to one request."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


LIB = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator/lib'
sys.path.insert(0, str(LIB))

import incremental_map_operator as operator
import incremental_map_supplement as supplemental
import incremental_source_retry as original


SELECTED = 'a' * 64
UNKNOWN = 'b' * 64
SOURCE = '10_Raw/source.pdf'
DIGEST = 'c' * 64


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    run = {'state': 'active', 'actor': 'operator', 'revision': 9,
           'source_manifest': [{'path': SOURCE, 'sha256': DIGEST}]}
    control = {'state': 'active', 'actor': 'operator', 'revision': 4}
    domain = SimpleNamespace(vault=tmp_path, status=lambda _: deepcopy(run))
    admissions = []
    monkeypatch.setattr(operator, 'IncrementalControl', lambda *_: SimpleNamespace(status=lambda: deepcopy(control)))
    monkeypatch.setattr(operator, 'service', lambda *_: domain)

    def inventory(_domain, run_id, path, *, request_ids=None):
        assert run_id == 'parent' and path == SOURCE
        assert not request_ids or request_ids == [SELECTED]
        mode = ('supplemental' if run['state'] == 'partial'
                else 'unavailable' if run['state'] == 'completed' or control['state'] == 'cancelled' else 'original')
        return {'mode': mode, 'reason': 'classified by original durable evidence',
                'evidence_fingerprint': f"{run['revision']}-{control['revision']}-101",
                'eligible': [{'request_id': SELECTED, 'task_id': 'original', 'reason': 'zero sends'}],
                'isolated': [{'request_id': UNKNOWN, 'task_id': 'unknown', 'reason': 'send evidence unavailable'}]}

    def retry(_vault, request):
        admissions.append(('original', request))
        assert request['request_ids'] == [SELECTED] and request['retry_kind'] == 'mapping-unconsumed'
        return {'state': 'approved', 'task_ids': ['original']}

    def supplement(_vault, request, *, preview=False):
        assert request['request_ids'] == [SELECTED]
        automatic = request.get('execution_scope') == 'automatic'
        if preview:
            report = {'report_sha256': 'child-admission', 'supplement_run_id': 'child'}
            if automatic:
                report.update(authorized_stages=list(operator.SUPPLEMENT_AUTOMATIC_STAGES), release_baseline=None)
            return report
        assert request['expected_report_sha256'] == 'child-admission'
        admissions.append(('supplemental', request))
        receipt = {'state': 'mapped', 'supplement_run_id': 'child'}
        if automatic:
            receipt['execution_scope'] = 'automatic'
        return receipt

    monkeypatch.setattr(original, 'recovery_inventory', inventory)
    monkeypatch.setattr(original, 'retry', retry)
    monkeypatch.setattr(supplemental, 'supplement', supplement)
    request = {'run_id': 'parent', 'actor': 'operator', 'operation_id': 'one-choice', 'source_path': SOURCE,
               'source_sha256': DIGEST, 'run_revision': 8, 'control_revision': 3}
    return SimpleNamespace(run=run, control=control, request=request, admissions=admissions, vault=tmp_path)


@pytest.mark.parametrize('run_state,control_state,allowed', [
    ('active', 'active', 'original'), ('active', 'paused', 'original'),
    ('active', 'cancelled', None), ('partial', 'active', 'supplemental'), ('completed', 'active', None),
])
def test_map_review_stays_read_only_across_run_states(recovery, run_state, control_state, allowed):
    recovery.run['state'], recovery.control['state'] = run_state, control_state
    report = operator.recover(recovery.vault, recovery.request, preview=True)
    assert report['request']['run_revision'] == 9 and report['request']['control_revision'] == 4
    assert [row['id'] for row in report['strategies'] if row['allowed']] == ([allowed] if allowed else [])
    assert any(row['request_id'] == UNKNOWN and row['disposition'] == 'isolated' for row in report['outcomes'])
    assert recovery.admissions == [] and list(recovery.vault.iterdir()) == []


@pytest.mark.parametrize('strategy', ['original', 'supplemental'])
def test_map_confirmation_rejects_changed_evidence_and_replays_one_admission(recovery, strategy):
    if strategy == 'supplemental':
        recovery.run['state'] = 'partial'
    report = operator.recover(recovery.vault, recovery.request, preview=True)
    request = {**report['request'], 'request_id': SELECTED, 'strategy': strategy,
               'expected_report_sha256': report['report_sha256']}
    recovery.run['revision'] += 1
    with pytest.raises(ValueError, match='preview changed'):
        operator.recover(recovery.vault, request)
    assert recovery.admissions == []

    fresh = operator.recover(recovery.vault, recovery.request, preview=True)
    request.update(run_revision=fresh['request']['run_revision'], expected_report_sha256=fresh['report_sha256'])
    with pytest.raises(ValueError, match='no longer qualifies'):
        operator.recover(recovery.vault, {**request, 'request_id': UNKNOWN})
    receipt = operator.recover(recovery.vault, request)
    recovery.run['revision'] += 1
    assert operator.recover(recovery.vault, request) == receipt
    assert [method for method, _ in recovery.admissions] == [strategy]
    assert receipt['strategy'] == strategy and receipt['request_id'] == SELECTED


def test_automatic_scope_and_child_linkage_are_bound_before_resuming(recovery, monkeypatch):
    import incremental_ingest_controller as controller

    recovery.run['state'] = 'partial'
    ordinary = operator.recover(recovery.vault, recovery.request, preview=True)
    automatic = operator.recover(recovery.vault, {**recovery.request, 'execution_scope': 'automatic'}, preview=True)
    assert ordinary['report_sha256'] != automatic['report_sha256']
    option, = [row for row in automatic['strategies'] if row['allowed']]
    assert option['authorized_stages'] == list(operator.SUPPLEMENT_AUTOMATIC_STAGES)
    assert option['execution_scope'] == 'automatic'

    request = {**automatic['request'], 'request_id': SELECTED, 'strategy': 'supplemental',
               'expected_report_sha256': automatic['report_sha256']}
    with pytest.raises(ValueError, match='preview changed'):
        operator.recover(recovery.vault, {key: value for key, value in request.items() if key != 'execution_scope'})
    assert recovery.admissions == []

    dispatched = []

    def resume(domain, child):
        path = domain.vault / '_system/ledgers/incremental-map-recoveries/parent/one-choice.json'
        assert operator._load_json(path)['state'] == 'committed'
        dispatched.append(child)
        if len(dispatched) == 1:
            raise OSError('launcher interrupted after Map')

    monkeypatch.setattr(controller, 'resume_run', resume)
    with pytest.raises(OSError, match='launcher interrupted'):
        operator.recover(recovery.vault, request)
    assert operator.recover(recovery.vault, request)['execution_scope'] == 'automatic'
    assert len(recovery.admissions) == 1 and dispatched == ['child', 'child']
