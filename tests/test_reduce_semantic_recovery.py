"""Native-transcript fixtures verify exact replay, not native semantic acceptance."""
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from fixed_semantic_recovery import recover_reduce
from fixed_semantic_schema import schema
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint


def call_fixture(tmp_path, phase='page-write'):
    home = tmp_path/'isolated-home'; home.mkdir()
    wid, node = 'fixture-workflow', 'global-reduce'
    binding = {'workflow_id':wid, 'node':node, 'task_id':'fresh-native', 'template_hash':'fixed-template'}
    wire = {'system':'Fixed fixture instructions', 'user':'Semantic input v1\n'+json.dumps({
        'phase':phase, 'input':{'evidence':'Original AND/OR, condition, exception and QA.',
                                 'parent':'Exact unchanged old page.', 'identity':'same-identity'}})}
    output_schema = schema(phase)
    draft = ({'content':'# Isolated fixture\nCondition retained.'} if phase == 'page-write' else
             {'pages':[], 'detail_unit_refs':[], 'reason':'Isolated proposal plan fixture'} if phase == 'global-plan' else
             {'pages':[], 'omitted_candidate_refs':[], 'reason':'Isolated plan fixture'} if phase.startswith('global') else
             {'proposals':[], 'omitted_candidate_refs':[], 'reason':'Isolated grouping fixture'})
    raw = json.dumps(draft)
    with sqlite3.connect(home/'state.db') as db:
        db.executescript('CREATE TABLE sessions(id TEXT,source TEXT); CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
        db.execute('INSERT INTO sessions VALUES (?,?)', ('fixture', 'kanban'))
        db.executemany('INSERT INTO messages VALUES (?,?,?,?)', [('fixture', 'user', wire['user'], None),
                                                                ('fixture', 'assistant', raw, None)])
    base = tmp_path/'_system/ledgers/ingest-workflows'/wid
    (base/'semantic-calls').mkdir(parents=True)
    (base/'execution-recoveries').mkdir()
    recovery_ref = (base/'execution-recoveries/retry.json').relative_to(tmp_path).as_posix()
    (tmp_path/recovery_ref).write_text(json.dumps({'request':{'recovery_scope':'reduce-cancel/v1'},
        'entries':[{'node':node, 'previous_task_id':'old-native', 'ended_run_ids':['7'], 'template_hash':'fixed-template'}]}))
    workflow = {'execution_recovery_history':[{'report_ref':recovery_ref}]}
    audit = {'workflow_id':wid, 'node':node, 'native_task_id':'old-native', 'native_run_id':'7',
             'template_hash':'fixed-template', 'phase':phase, 'schema_sha256':fingerprint(output_schema),
             'input_sha256':fingerprint(wire), 'user_sha256':fingerprint(wire['user']), 'response_sha256':fingerprint(raw),
             'model':{'origin':'hermes_native_model', 'session_id':'fixture', 'response_sha256':fingerprint(raw)},
             'receipt_refs':[]}
    audit_path = base/'semantic-calls/original.json'; audit_path.write_text(json.dumps(audit))
    checks = []
    adapter = SimpleNamespace(worker_check=lambda b:checks.append(b) or {'ok':True},
        workflow=SimpleNamespace(vault=tmp_path, status=lambda _:workflow, assert_execution_ready=lambda _:None))
    return SimpleNamespace(home=home, binding=binding, adapter=adapter, wire=wire, output_schema=output_schema,
                           draft=draft, audit=audit, audit_path=audit_path, checks=checks, workflow=workflow)


def replay(c, **changes):
    return recover_reduce(c.adapter, c.binding, changes.get('phase', c.audit['phase']),
        changes.get('wire', c.wire), changes.get('output_schema', c.output_schema), changes.get('images', []),
        lambda raw:(json.loads(raw), None), home=c.home)


@pytest.mark.parametrize('phase', ['resource-reduce','global-reduce','global-plan','global-detail','page-write'])
def test_exact_native_reduce_response_reused_under_fresh_checked_authority(tmp_path, phase):
    c = call_fixture(tmp_path, phase)
    before = c.audit_path.read_bytes()
    draft, ref = replay(c)
    assert draft == c.draft and len(c.checks) == 2
    recovered = json.loads((tmp_path/ref).read_text())
    assert recovered['model_elapsed_ms'] == 0 and recovered['native_task_id'] == c.binding['task_id']
    assert recovered['reused_from_audit'] == c.audit_path.relative_to(tmp_path).as_posix()
    assert c.audit_path.read_bytes() == before and not recovered['receipt_refs']


@pytest.mark.parametrize('change', ['wire','schema','image','task','node','template','run','response','origin',
                                   'session','tools','rejected','refusal','reused','scope'])
def test_reduce_replay_rejects_stale_untrusted_or_rejected_inputs(tmp_path, change):
    c = call_fixture(tmp_path)
    args = {}
    if change == 'wire': args['wire'] = {**c.wire, 'user':c.wire['user']+'Changed parent or original evidence.'}
    elif change == 'schema': args['output_schema'] = {'type':'string'}
    elif change == 'image': args['images'] = [{'sha256':'changed-image','references':[],'url':'data:fixture'}]
    elif change == 'task': c.audit['native_task_id'] = 'unrelated-native'
    elif change == 'node': c.audit['node'] = 'resource-reduce:foreign'
    elif change == 'template': c.audit['template_hash'] = 'changed-template'
    elif change == 'run': c.audit['native_run_id'] = '8'
    elif change == 'response': c.audit['response_sha256'] = 'tampered'
    elif change == 'origin': c.audit['model']['origin'] = 'isolated_fixture'
    elif change in ('session','tools'):
        with sqlite3.connect(c.home/'state.db') as db:
            if change == 'session': db.execute("UPDATE sessions SET source='untrusted'")
            else: db.execute("UPDATE messages SET tool_calls='[{}]' WHERE role='user'")
    elif change == 'rejected': c.audit['validated'] = False
    elif change == 'refusal':
        raw = json.dumps({'blocked_reason':'Required evidence missing'})
        c.audit['response_sha256'] = c.audit['model']['response_sha256'] = fingerprint(raw)
        with sqlite3.connect(c.home/'state.db') as db: db.execute("UPDATE messages SET content=? WHERE role='assistant'", (raw,))
    elif change == 'reused': c.audit['reused_from_audit'] = 'earlier.json'
    elif change == 'scope': c.workflow['execution_recovery_history'] = []
    c.audit_path.write_text(json.dumps(c.audit))
    assert replay(c, **args) is None


def test_reduce_replay_rechecks_current_authority_before_reuse(tmp_path):
    c = call_fixture(tmp_path)
    count = 0
    def checked(binding):
        nonlocal count
        count += 1
        if count == 2: raise ContractError('WORKFLOW_STOPPED', '$', 'cancelled during replay verification')
    c.adapter.worker_check = checked
    with pytest.raises(ContractError, match='WORKFLOW_STOPPED'): replay(c)


def test_source_hold_authorizes_exact_native_replay_with_persisted_image_marker(tmp_path):
    c=call_fixture(tmp_path,'resource-reduce')
    ref=c.workflow['execution_recovery_history'][0]['report_ref']
    report=json.loads((tmp_path/ref).read_text());report['request']['recovery_scope']='source-hold/v1'
    (tmp_path/ref).write_text(json.dumps(report))
    images=[{'sha256':'original-image','references':[]}]
    c.audit['image_inputs']=images;c.audit_path.write_text(json.dumps(c.audit))
    with sqlite3.connect(c.home/'state.db') as db:
        db.execute("UPDATE messages SET content=content || ? WHERE role='user'",('\n[screenshot]',))
    assert replay(c,images=images)[0]==c.draft
    assert replay(c,images=[{**images[0],'sha256':'changed'}]) is None
