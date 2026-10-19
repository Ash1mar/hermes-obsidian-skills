"""Evidence representation preserves facts and consumed attempt identities."""
import copy,json
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
import fixed_page_evidence as e
from fixed_semantic import prepare_request_projection,wire_prompt
from fixed_semantic_schema import schema
from semantic_output import path_for


def view():
    qa=[{'qa':'needs-qa','finding':'original limitation '*30,'qa_note':'retain exact limitation'}]
    return {'identity':{'kind':'fact','identity_key':'topic','canonical_name':'Topic','aliases':[]},'path':'topic.md','existing_page':None,
        'candidates':[{'candidate':'c'+str(i),'finding':'a fact','conditions':['A OR B'],'exceptions':['C'],
         'evidence_qa':copy.deepcopy(qa),'source_evidence':[{'reading':'t1','support_refs':['m1']}]} for i in range(8)],
        'original_evidence':{'tasks':[{'task':'t1','limits':{'omitted_material_count':3},'materials':[
            {'ref':'m'+str(i),'role':'core' if i<2 else 'context','heading':1,'text_ref':'x','source_span':{'start':i,'end':i+1},
             **({'asset':None} if i==0 else {}),**({'required_dependency':True} if i==1 else {})} for i in range(12)]}],
         'headings':{'1':['Standard','Section']},'texts':{'x':'Exact original 12 mm AND 13 mm. ' *100},'qa':{'q1':{'actual':'retain'}}}}


def decoded_table(t):
    missing={tuple(p) for p in t.get('missing',[])}
    return [{k:r[j] for j,k in enumerate(t['columns']) if (i,j) not in missing} for i,r in enumerate(t['rows'])]


def fixture(tmp_path):
    return NS(workflow=NS(vault=tmp_path,status=lambda _:{'execution_recovery_history':[]})),{'workflow_id':'fixture','node':'global-reduce'},view()


def test_encoding_roundtrips_full_materials_missing_vs_null_and_candidate_qa():
    old=view();encoded=e.present({**old,e.MARKER:e.CONTRACT})
    task=encoded['original_evidence']['tasks'][0]
    assert decoded_table(task['materials'])==old['original_evidence']['tasks'][0]['materials']
    assert task['limits']==old['original_evidence']['tasks'][0]['limits']
    assert encoded['original_evidence']['texts']==old['original_evidence']['texts']
    assert encoded['original_evidence']['headings']==old['original_evidence']['headings']
    for actual,original in zip(encoded['candidates'],old['candidates']):
        assert {**actual,'evidence_qa':encoded['candidate_qa_pool'][actual['evidence_qa']]}==original
    assert e.present({**old,e.MARKER:e.CONTRACT})==encoded and view()==old


def test_only_marked_page_wires_change_and_full_instruction_is_metered(tmp_path):
    a,b,v=fixture(tmp_path);raw=prepare_request_projection('page-write',v,schema('page-write'),[])
    selected=e.select(a,b,'page-write',v,prepare_request_projection)
    assert selected[e.MARKER]==e.CONTRACT
    packed=prepare_request_projection('page-write',selected,schema('page-write'),[])
    assert packed['input_size']<raw['input_size'] and e.INSTRUCTION in packed['wire']['system']
    assert json.loads(raw['wire']['user'].split('\n',1)[1])['input']==v
    body=json.loads(packed['wire']['user'].split('\n',1)[1])['input']
    assert decoded_table(body['original_evidence']['tasks'][0]['materials'])==v['original_evidence']['tasks'][0]['materials']


@pytest.mark.parametrize('history_state',['valid','rejected','in_flight'])
def test_consumed_original_keeps_wire_and_attempt_bytes(tmp_path,history_state):
    a,b,v=fixture(tmp_path);p=path_for(a,b,'page-write',v);p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'attempts':[{'attempt':1,'state':history_state}]}));before=p.read_bytes()
    assert e.select(a,b,'page-write',v,prepare_request_projection)==v and p.read_bytes()==before
    assert not path_for(a,b,'page-write',{**v,e.MARKER:e.CONTRACT}).exists()


def test_encoded_failure_cannot_fall_back_to_a_new_original_allowance(tmp_path):
    a,b,v=fixture(tmp_path);new={**v,e.MARKER:e.CONTRACT};p=path_for(a,b,'page-write',new);p.parent.mkdir(parents=True)
    p.write_text('{"attempts":[{"attempt":1,"state":"rejected"},{"attempt":2,"state":"rejected"}]}');before=p.read_bytes()
    assert e.select(a,b,'page-write',v,prepare_request_projection)==new and p.read_bytes()==before
    old=path_for(a,b,'page-write',v);old.write_text('{"attempts":[{}]}')
    with pytest.raises(ContractError,match='both page evidence encodings'):e.select(a,b,'page-write',v,prepare_request_projection)


def test_small_inputs_keep_original_when_decoder_instruction_cost_exceeds_saving(tmp_path):
    a,b,_=fixture(tmp_path);v={'candidates':[],'original_evidence':{'tasks':[]}}
    assert e.select(a,b,'page-write',v,prepare_request_projection)==v


def test_review_uses_identical_encoding_without_rewriting_draft_or_parent():
    v=view();v['citation_evidence']=v.pop('candidates');v.update(content='# Draft\nExact content',existing_page={'content':'# Parent','path':'old.md'})
    p=e.present({**v,e.MARKER:e.CONTRACT})
    assert p['content']==v['content'] and p['existing_page']==v['existing_page']
    assert p['citation_evidence'][0]['evidence_qa'] in p['candidate_qa_pool']
    assert e.INSTRUCTION in wire_prompt('page-review',{**v,e.MARKER:e.CONTRACT},schema('page-review'))['system']
