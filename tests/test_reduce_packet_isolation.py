"""A local native output failure holds source submission, not unrelated packet calls."""
from types import SimpleNamespace
import json
import pytest

import fixed_semantic as fixed
from hermes_source_units import ContractError
from test_reduce_packets import value


@pytest.mark.parametrize('code,audited,expected_calls',[
    ('SEMANTIC_REVIEW_REQUIRED',True,3),('INVALID_SCHEMA',True,3),
    ('INVALID_SCHEMA',False,1),('ACCESS_DENIED',True,1),('READING_WINDOW_OVERSIZE',False,1)])
def test_packet_refusal_preserves_successes_and_attempts_remaining_without_retry(tmp_path,monkeypatch,code,audited,expected_calls):
    source=value(16000);source.update(input_id='isolated',backend={'kind':'resource-reduce'})
    binding={'workflow_id':'isolated','node':'resource-reduce:source','task_id':'native-fixture'}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,knowledge=SimpleNamespace(source=None)),
                            worker_check=lambda _: {'ok':True})
    monkeypatch.setattr(fixed,'canonical_binding',lambda *a,**k:(binding,None))
    monkeypatch.setattr(fixed.fixed_reduce,'prepare',lambda *a:('isolated-source-input.json',source))
    def forbidden(*args):raise AssertionError('a held source must not submit or complete')
    monkeypatch.setattr(fixed.fixed_reduce,'submit',forbidden);adapter.worker_complete=forbidden
    calls=[];receipts=[]
    def model(adapter,binding,phase,view,caller,limit,**kwargs):
        assert kwargs['attempt_limit']==1
        handles=[c['candidate'] for c in view['candidates']];calls.append(handles)
        if len(calls)==1:
            error=ContractError(code,'$','Isolated native result failure')
            if audited:error.semantic_audit_ref='isolated-bad-native-audit'
            raise error
        return {'proposals':[{'candidate_refs':handles,'summary':'Supported isolated proposal',
            'identity_hints':[],'path_hints':[]}],'omitted_candidate_refs':[],'reason':'Isolated native fixture'},'isolated-good-'+str(len(calls))
    monkeypatch.setattr(fixed,'model_call',model)
    monkeypatch.setattr(fixed,'attach_receipt',lambda a,r,v:receipts.append((r,v)))
    with pytest.raises(ContractError):fixed.execute(adapter,'isolated-binding',caller=object())
    assert len(calls)==expected_calls and len({tuple(c) for c in calls})==len(calls)
    reports=list(tmp_path.rglob('source-packets-deferred-*.json'))
    if expected_calls==3:
        assert len(reports)==1
        report=json.loads(reports[0].read_text())
        assert report['completed_packet_count']==2 and report['source_submitted'] is False
        assert report['unchanged_semantic_retries']==0 and report['held_packets'][0]['candidate_refs']==['c1']
        assert receipts[0][1]['validated'] is False
    else:assert not reports
