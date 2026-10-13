"""Finite old/new native framing compatibility with unchanged reuse authority."""
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from fixed_semantic_recovery import recover
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint


def audited_call(tmp_path, *, header='Semantic input v1', tail='\nImage 1: t1/m1', images=True):
    home = tmp_path / 'isolated-home'
    home.mkdir()
    workflow_id = 'isolated-workflow'
    binding = {'workflow_id': workflow_id, 'task_id': 'fresh-native',
               'node': 'pass-slice:new', 'template_hash': 'current'}
    task = {'task': 't1', 'materials': [{'ref': 'm1', 'role': 'core',
             'text_ref': 'c1', 'heading': 0, 'qa_ref': 'q1'}],
            'candidate': {'candidates': []}, 'limits': {}}
    view = {'tasks': [task], 'texts': {'c1': 'Condition AND exception.'},
            'headings': {'0': ['Scope']}, 'qa': {'q1': [{'reason': 'Source limit.'}]}}
    body = {'phase': 'citation', 'input': view, 'required_result_slots': ['t1']}
    user = header + '\n' + json.dumps(body, ensure_ascii=False, separators=(',', ':')) + tail
    raw = json.dumps({'reviews': [{'task': 't1', 'decision': 'confirmed_unchanged',
                                  'review_note': 'Source reviewed.'}]})
    with sqlite3.connect(home / 'state.db') as db:
        db.executescript('CREATE TABLE sessions(id TEXT,source TEXT);'
                         'CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
        db.execute('INSERT INTO sessions VALUES (?,?)', ('fixture', 'kanban'))
        db.executemany('INSERT INTO messages VALUES (?,?,?,?)',
                       [('fixture', role, content, None) for role, content in
                        [('user', user), ('assistant', raw)]])
    root = tmp_path / '_system/ledgers/ingest-workflows' / workflow_id / 'semantic-calls'
    root.mkdir(parents=True)
    audit = {'workflow_id': workflow_id, 'phase': 'citation', 'native_task_id': 'prior-native',
             'node': 'pass-slice:old', 'user_sha256': fingerprint(user),
             'response_sha256': fingerprint(raw),
             'input_sha256': fingerprint({'system': 'fixed system', 'user': user}),
             'image_inputs': [{'references': [{'task': 't1', 'material': 'm1'}]}] if images else [],
             'model': {'origin': 'hermes_native_model', 'session_id': 'fixture',
                       'response_sha256': fingerprint(raw)}}
    audit_path = root / 'old.json'
    audit_path.write_text(json.dumps(audit))
    checks = []

    def worker_check(actual):
        checks.append(actual)
        return {'batch_id': 'b', 'task_ids': ['same-task']}

    adapter = SimpleNamespace(worker_check=worker_check,
        workflow=SimpleNamespace(vault=tmp_path,
            knowledge=SimpleNamespace(_slice=lambda *args: {'task_ids': ['same-task']})))
    return SimpleNamespace(home=home, binding=binding, view=view, audit=audit,
                           audit_path=audit_path, adapter=adapter, checks=checks)


def replay(call, *, view=None, system='fixed system'):
    return recover(call.adapter, call.binding, 'citation', view or call.view,
                   lambda raw: (json.loads(raw), None), system, home=call.home)


@pytest.mark.parametrize('header', ['Semantic input v1', 'Native task prior-native'])
@pytest.mark.parametrize('images', [False, True])
def test_supported_framing_reuses_only_real_checked_native_response(tmp_path, header, images):
    call = audited_call(tmp_path, header=header, images=images,
                        tail='\nImage 1: t1/m1' if images else '')
    result = replay(call)
    assert result[0]['reviews'][0]['task'] == 't1'
    assert result[0]['slot_failures'] == []
    assert call.checks == [call.binding]
    record = json.loads((tmp_path / result[1]).read_text())
    assert record['reused_from_audit'] == call.audit_path.relative_to(tmp_path).as_posix()
    assert record['input_sha256'] == call.audit['input_sha256']
    assert record['response_sha256'] == call.audit['response_sha256']


@pytest.mark.parametrize('header', ['', 'Other input v1', 'Semantic input v2',
                                    'Semantic input v1 ', 'Native task ', 'Native task  ',
                                    'Native task prior native'])
def test_unknown_header_cannot_be_recovered_by_searching_for_json(tmp_path, header):
    assert replay(audited_call(tmp_path, header=header)) is None


@pytest.mark.parametrize('tail', ['\nUnknown tail', '\nImage 1: t1/m2',
                                  '\nImage 1: t1/m1\nUnknown tail',
                                  '\nNative task prior-native', '\nImage 1: t1/m1\n'])
def test_only_exact_registered_image_label_tail_is_accepted(tmp_path, tail):
    assert replay(audited_call(tmp_path, tail=tail)) is None


@pytest.mark.parametrize('changed', ['system', 'material', 'qa', 'response_hash',
                                    'input_hash', 'user_hash', 'session_source', 'tools'])
def test_framing_does_not_weaken_native_hash_or_evidence_requirements(tmp_path, changed):
    call = audited_call(tmp_path)
    view, system = copy.deepcopy(call.view), 'fixed system'
    if changed == 'system':
        system = 'changed fixed instructions'
    elif changed == 'material':
        view['texts']['c1'] = 'Changed OR scope.'
    elif changed == 'qa':
        view['qa']['q1'] = [{'reason': 'Different source limit.'}]
    elif changed in ('response_hash', 'input_hash', 'user_hash'):
        call.audit[{'response_hash': 'response_sha256', 'input_hash': 'input_sha256',
                    'user_hash': 'user_sha256'}[changed]] = fingerprint('tampered')
        call.audit_path.write_text(json.dumps(call.audit))
    else:
        with sqlite3.connect(call.home / 'state.db') as db:
            if changed == 'session_source':
                db.execute("UPDATE sessions SET source='untrusted'")
            else:
                db.execute("UPDATE messages SET tool_calls='[{}]' WHERE role='user'")
    assert replay(call, view=view, system=system) is None


def test_recovery_still_requires_current_worker_lease(tmp_path):
    call = audited_call(tmp_path)

    def stopped(binding):
        raise ContractError('LEASE_EXPIRED', '$', 'isolated expired native lease')

    call.adapter.worker_check = stopped
    with pytest.raises(ContractError, match='LEASE_EXPIRED'):
        replay(call)
