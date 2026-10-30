"""Negative cases for disposition accounting; no real Vault or model calls."""
import copy
import json
from types import SimpleNamespace as NS

import pytest
import build_disposition as disposition
import semantic_output
from hermes_source_units import ContractError
from test_build_acceptance import execution
import fixed_review


def test_disputed_review_is_never_automatically_approved_and_later_page_survives(tmp_path, monkeypatch):
    adapter, binding, views, submitted = execution(tmp_path, monkeypatch, [['different_applicability']]*2)
    calls = []
    def checked(_a, _b, phase, view, *args, **kwargs):
        calls.append(view['page'])
        return {'decision': 'revision_required' if len(calls) == 1 else 'approved',
                'review_note': 'No substantive defect found.'}, 'native-review', None
    monkeypatch.setattr(semantic_output, 'execute', checked)
    with pytest.raises(ContractError, match='SEMANTIC_REVIEW_REQUIRED'):
        fixed_review.execute(adapter, binding, object(), None, lambda *_: None)
    report = json.loads(next(tmp_path.rglob('build-dispositions/*.json')).read_text())
    assert calls == ['e0', 'e1'] and submitted == []
    assert report['ready_count'] == 1 and report['deferred_count'] == 1
    disputed = report['items'][0]
    assert disputed['original_decision']['decision'] == 'revision_required'
    assert disputed['attribution']['confirmed_cause'] is None
    assert disputed['retry_policy']['unchanged_input_new_calls'] == 0
    assert report['finalized'] is False and report['publication_authorized'] is False
    assert len(report['prepared_reviews']) == 1


@pytest.mark.parametrize('stopped', [False, True])
def test_missing_native_completion_is_not_a_content_error_or_no_call(tmp_path, stopped):
    adapter = NS(workflow=NS(vault=tmp_path))
    binding = {'workflow_id': 'fixture', 'node': 'build-finalize', 'task_id': 'dispatch-one'}
    view = {'page': 'e1'}
    path = semantic_output.path_for(adapter, binding, 'page-review', view)
    path.parent.mkdir(parents=True)
    attempt = {'attempt_id': 'consumed', 'state': 'rejected',
               'diagnostic': {'code': 'WORKFLOW_STOPPED' if stopped else 'NETWORK_ERROR'}}
    path.write_text(json.dumps({'attempts': [attempt]}))
    value = {'backend': {'pages': {'e1': {'run_id': 'canonical-run', 'page': {'page_id': 'page',
        'revision_id': 'original', 'authored_sha256': 'a'*64}}}, 'runs': []}}
    item = disposition.item(adapter, binding, value, view, state='deferred',
        error=ContractError('SEMANTIC_OUTPUT_HELD', '$', 'unknown'))
    changed = disposition.item(adapter, {**binding, 'task_id': 'dispatch-two'}, value, view,
        state='deferred', error=ContractError('SEMANTIC_OUTPUT_HELD', '$', 'unknown'))
    assert item['issue_id'] == changed['issue_id']
    assert item['observed_status'] == ('unproven_stopped_attempt' if stopped else 'completion_unknown')
    assert item['attribution']['candidates'] == ['execution_uncertainty']
    assert item['history']['attempt_count'] == 1
    assert item['retry_policy']['new_calls_authorized'] is False


def test_incomplete_partition_cannot_create_ready_manifest(tmp_path):
    adapter = NS(workflow=NS(vault=tmp_path))
    value = {'backend': {'pages': {'e1': {}}}}
    with pytest.raises(ContractError, match='INCOMPLETE_COVERAGE'):
        disposition.persist(adapter, {}, value, 'unused', [], {}, {})
    assert not list(tmp_path.rglob('build-dispositions/*.json'))
