"""Full historical rejection/correction proof without writes or new calls."""
import json
import pytest
from hermes_source_units import ContractError
from semantic_output import read_saved_output,path_for,check
import fixed_global_packets as g
from test_semantic_output import fixture,answer


def saved_fixture(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer()]);expected=c.run()
    path=path_for(c.adapter,c.binding,'global-plan',c.packet['view']);j=json.loads(path.read_text())
    for attempt in j['attempts']:
        p=tmp_path/attempt['audit_ref'];p.write_text(json.dumps({'phase':'global-plan','native_task_id':attempt['task_id'],
            'native_run_id':attempt['run_id'],'output_attempt_id':attempt['attempt_id']}))
    prove=lambda path,*_:(json.dumps(c.saved[path.name]),{})
    # The fixture value supplies proposal membership and full cumulative checks.
    from test_global_plan_inputs import fixture_value
    validator=lambda draft:check('global-plan',fixture_value(),c.packet,g.initial(),draft)
    return c,path,j,prove,validator,expected


def test_saved_planning_preserves_rejected_attempt_and_exact_final_success(tmp_path,monkeypatch):
    c,path,j,prove,validator,expected=saved_fixture(tmp_path,monkeypatch)
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result=read_saved_output(c.adapter,c.binding,'global-plan',c.packet['view'],validator,prove,'audit-2')
    assert result==expected and len(c.calls)==2 and before=={p:p.read_bytes() for p in before}


@pytest.mark.parametrize('change',['drop-rejection','wire','ordinal','task','extra-attempt','proof'])
def test_saved_planning_rejects_changed_history_without_rewriting_it(tmp_path,monkeypatch,change):
    c,path,j,prove,validator,_=saved_fixture(tmp_path,monkeypatch)
    if change=='drop-rejection':j['attempts']=j['attempts'][1:]
    if change=='wire':j['attempts'][0]['wire_sha256']='different'
    if change=='ordinal':j['attempts'][0]['attempt_id']='different'
    if change=='task':j['attempts'][0]['task_id']='different'
    if change=='extra-attempt':j['attempts'].append(dict(j['attempts'][-1]))
    if change=='proof':prove=lambda *_:None
    path.write_text(json.dumps(j));before=path.read_bytes()
    with pytest.raises(ContractError,match='SEMANTIC_OUTPUT_HELD'):
        read_saved_output(c.adapter,c.binding,'global-plan',c.packet['view'],validator,prove,'audit-2')
    assert path.read_bytes()==before and len(c.calls)==2
