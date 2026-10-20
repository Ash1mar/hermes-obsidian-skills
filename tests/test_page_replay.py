"""Isolated saved page proof fixtures, never native semantic acceptance."""
import copy,json
from types import SimpleNamespace as NS
import pytest
import fixed_page_replay as replay
from fixed_semantic import prepare_request_projection
from fixed_semantic_schema import schema_for
from semantic_output import path_for
from hermes_source_units.validation import fingerprint


def fixture(tmp_path,answers):
    a=NS(workflow=NS(vault=tmp_path));b={'workflow_id':'fixture','node':'global-reduce'}
    view={'identity':{'kind':'fact','identity_key':'x','canonical_name':'X','aliases':[]},'path':'x.md','candidates':[],'existing_page':None}
    schema=schema_for('page-write',view);attempts=[];raws={};path=path_for(a,b,'page-write',view)
    for i,(answer,state,correctable) in enumerate(answers,1):
        correction=None if i==1 else {'code':'INVALID_SCHEMA','instruction':'Same evidence'}
        prepared=prepare_request_projection('page-write',view,schema,[],correction);ref=f'audit-{i}'
        raws[ref]=json.dumps(answer)
        audit=tmp_path/ref;audit.write_text(json.dumps({'phase':'page-write','native_task_id':'task','native_run_id':'run'}))
        attempts.append({'attempt':i,'attempt_id':fingerprint({'path':path.name,'attempt':i}),'task_id':'task','run_id':'run','state':state,'correctable':correctable,'audit_ref':ref,
            'wire_sha256':fingerprint(prepared['wire']),'correction':correction})
    record={'contract':'finite-semantic-output/v1','phase':'page-write','input_sha256':fingerprint(view),
        'schema_sha256':fingerprint(schema),'attempts':attempts}
    path=path_for(a,b,'page-write',view);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(record))
    def prove(path,*_):return (raws[path.name],{})
    return a,b,view,path,prove


def test_saved_body_proof_reuses_exact_last_valid_content_without_journal_writes(tmp_path):
    a,b,v,path,prove=fixture(tmp_path,[({'content':'# Exact native fixture'},'valid',False)])
    before=path.read_bytes();result=replay.read_outcome(a,b,v,prove,[])
    assert result['kind']=='valid' and result['draft']['content']=='# Exact native fixture' and path.read_bytes()==before
    record=json.loads(path.read_text());record['attempts'][0]['wire_sha256']='changed';path.write_text(json.dumps(record))
    assert replay.read_outcome(a,b,v,prove,[]) is None


def test_uncertain_second_reservation_never_becomes_a_valid_body_or_new_call(tmp_path):
    a,b,v,path,prove=fixture(tmp_path,[({'unexpected':'wrong output'},'rejected',True)])
    record=json.loads(path.read_text());record['attempts'].append({'attempt':2,'attempt_id':fingerprint({'path':path.name,'attempt':2}),'state':'reserved','session_id':'unknown'})
    path.write_text(json.dumps(record));before=path.read_bytes()
    result=replay.read_outcome(a,b,v,prove,[])
    assert result['kind']=='pending' and result['error'].code=='INVALID_SCHEMA' and path.read_bytes()==before


def test_unused_correction_stays_in_normal_queue_but_semantic_refusal_is_sealed(tmp_path):
    a,b,v,path,prove=fixture(tmp_path,[({'unexpected':'wrong'},'rejected',True)])
    assert replay.read_outcome(a,b,v,prove,[]) is None
    a,b,v,path,prove=fixture(tmp_path,[({'blocked_reason':'Original evidence insufficient'},'rejected',False)])
    result=replay.read_outcome(a,b,v,prove,[])
    assert result['kind']=='held' and result['error'].code=='SEMANTIC_REVIEW_REQUIRED'


@pytest.mark.parametrize('field',['attempt_id','task_id','run_id'])
def test_bulk_reuse_does_not_rebind_saved_attempt_identity(tmp_path,field):
    a,b,v,path,prove=fixture(tmp_path,[({'content':'# Exact'},'valid',False)])
    record=json.loads(path.read_text());record['attempts'][0][field]='changed';path.write_text(json.dumps(record))
    assert replay.read_outcome(a,b,v,prove,[]) is None
