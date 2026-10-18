"""Isolated routing/writing contract checks, not native semantic acceptance."""
import copy,json
from types import SimpleNamespace
import pytest
import fixed_reduce
import fixed_semantic as fixed
from hermes_source_units import ContractError


def fixture_value(oversize=False):
    candidates=[{'candidate':'c'+str(n),'kind':'requirement','name':'Version '+str(n),'finding':'Full fact '+str(n),
        'applicability':'Source version '+str(n),'conditions':['A OR B'],'exceptions':['unless C'],
        'evidence_qa':[{'qa':'usable','qa_note':'','finding':'Repeated inspection '*20},
                       {'qa':'needs-qa','qa_note':'formula uncertain','finding':'Do not assert equation'}],
        'source_evidence':[{'reading':'t'+str(n),'support_refs':['m1']}]} for n in (1,2)]
    packet={'texts':{'s1':'Complete original AND/OR conditions.','s2':'x'*40000 if oversize else 'Second original version.'},
        'headings':[['First'],['Second']],'qa':{'q1':['Source QA'], 'q2':['Other source QA']},
        'tasks':[{'task':'t'+str(n),'materials':[{'ref':'m1','text_ref':'s'+str(n),'heading':n-1,'role':'core',
            'qa_ref':'q'+str(n),'permission':'this original only'}]} for n in (1,2)]}
    return {'input_id':'a'*64,'view':{'phase':'global-reduce','candidates':candidates,'original_evidence':packet,
        'proposals':[{'proposal':'p'+str(n),'candidate_refs':['c'+str(n)],'summary':'Source group '+str(n),
                      'identity_hints':[],'path_hints':[]} for n in (1,2)],'existing_pages':[],
        'coverage_limitations':{'excluded_task_count':15}},'backend':{'kind':'global-reduce','existing_pages':{},
        'batch_id':'fixture','actor':'agent','tasks':[],'mapping':{'c1':[{'pass_id':'b'*64,'candidate_id':'one'}],
        'c2':[{'pass_id':'c'*64,'candidate_id':'two'}]},'identities':{'revision':1},'document_registry_revision':1,'reductions':[]}}


def test_provisional_routing_uses_proposals_while_full_boundaries_stay_in_backend():
    value=fixture_value();before=copy.deepcopy(value);view=fixed_reduce.global_plan_view(value)
    proposals=[dict(zip(view['columns']['proposals'],row)) for row in view['proposals']]
    assert [p['proposal'] for p in proposals]==['p1','p2'] and all(p['member_count']==1 for p in proposals)
    assert 'candidates' not in view and 'qa' not in view and 'original_evidence' not in view
    assert all('candidate_refs' not in p for p in proposals)
    assert 'Repeated inspection' not in json.dumps(view) and 'Do not assert equation' not in json.dumps(view)
    assert list(view['risks'].values())==[['needs-qa','formula uncertain']]
    assert proposals[0]['risks']==proposals[1]['risks'] and view['existing_pages']==[]
    assert view['coverage_limitations']==value['view']['coverage_limitations'] and value==before
    detail=fixed_reduce.global_detail_view(value)
    candidates=[dict(zip(detail['columns']['candidates'],row)) for row in detail['candidates']]
    assert all(c['conditions']==['A OR B'] and c['exceptions']==['unless C'] for c in candidates)
    assert candidates[0]['finding']=='Full fact 1'
    assert detail['qa'][candidates[0]['evidence_qa'][1]]['finding']=='Do not assert equation'


@pytest.mark.parametrize('mode',['valid','oversize','blocked'])
def test_new_pages_always_write_full_evidence_and_preflight_all_bodies(tmp_path,monkeypatch,mode):
    value=fixture_value(mode=='oversize');binding={'node':'global-reduce','workflow_id':'fixture','task_id':'fixture'}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,status=lambda _: {'execution_recovery_history':[]}),worker_check=lambda *_:None,worker_complete=lambda *_:{'ok':True})
    monkeypatch.setattr(fixed,'canonical_binding',lambda *_a,**_k:(binding,{}))
    monkeypatch.setattr(fixed_reduce,'prepare',lambda *_:('fixture',value))
    monkeypatch.setattr(fixed,'attach_receipt',lambda *_:None)
    monkeypatch.setattr(fixed_reduce,'submit',lambda *_:{'ok':True})
    phases=[]
    def call(_adapter,_binding,phase,view,*_a,**_k):
        phases.append(phase)
        if phase=='global-plan':
            assert 'original_evidence' not in view
            return {'pages':[{'identity':{'kind':'entity','identity_key':'v'+str(n),'canonical_name':'Version '+str(n),'aliases':[]},
                'path':'30_Cards/v'+str(n)+'.md','proposal_refs':['p'+str(n)]} for n in (1,2)],
                'detail_unit_refs':[],'reason':'Preserve p1 and p2 as separate versions'},'fixture-call'
        assert phase=='page-write' and len(view['candidates'])==1 and view['existing_page'] is None
        assert len(view['original_evidence']['tasks'])==1 and view['original_evidence']['qa']
        assert view['original_evidence']['tasks'][0]['materials'][0]['permission']=='this original only'
        assert view['coverage_limitations']['excluded_task_count']==15
        assert view['candidates'][0]['finding'].startswith('Full fact')
        if mode=='blocked':return {'blocked_reason':'Planned identity cannot preserve original scope'},'fixture-write'
        return {'content':'Reviewed full evidence'},'fixture-write'
    monkeypatch.setattr(fixed,'model_call',call)
    if mode=='oversize':
        with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):fixed.execute(adapter,'fixture',lambda:None)
        assert phases==['global-plan']
    elif mode=='blocked':
        with pytest.raises(ContractError,match='SEMANTIC_REVIEW_REQUIRED'):fixed.execute(adapter,'fixture',lambda:None)
        assert phases==['global-plan','page-write']
    else:
        assert fixed.execute(adapter,'fixture',lambda:None)['ok']
        assert phases==['global-plan','page-write','page-write']


def test_only_explicit_detail_request_adds_a_local_call(tmp_path,monkeypatch):
    value=fixture_value();binding={'node':'global-reduce','workflow_id':'fixture','task_id':'fixture'}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,status=lambda _: {'execution_recovery_history':[]}),worker_check=lambda *_:None,worker_complete=lambda *_:{'ok':True})
    monkeypatch.setattr(fixed,'canonical_binding',lambda *_a,**_k:(binding,{}))
    monkeypatch.setattr(fixed_reduce,'prepare',lambda *_:('fixture',value))
    monkeypatch.setattr(fixed,'attach_receipt',lambda *_:None)
    submissions=[];monkeypatch.setattr(fixed_reduce,'submit',lambda *a:submissions.append(a[-1]) or {'ok':True})
    from test_incremental_global_reduce import proposal_page,proposal_response,page,response
    phases=[]
    def call(_adapter,_binding,phase,view,*_a,**_k):
        phases.append(phase)
        if phase=='global-plan':
            assert 'candidates' not in view
            return proposal_response([proposal_page(['p1'],'v1')],['u2']),'plan'
        if phase=='global-detail':
            assert [r[0] for r in view['candidates']]==['c2']
            assert 'version-specific split' in view['coordination']['detail_reason']
            return response([page('c2','v2')]),'detail'
        assert phase=='page-write' and view['original_evidence']['texts']
        return {'content':'Full source condition preserved'},'body'
    monkeypatch.setattr(fixed,'model_call',call)
    assert fixed.execute(adapter,'fixture',lambda:None)['ok']
    assert phases==['global-plan','global-detail','page-write','page-write']
    assert {r for p in submissions[0]['pages'] for r in p['candidate_refs']}=={'c1','c2'}
