"""Uncalled packing recovery cannot authorize retry or uncertain completion."""
import json
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
import semantic_output_recovery as recovery
import fixed_semantic
from fixed_global_targets import target_view
from test_global_target_refs import packing_fixture
import fixed_global_packets as g


@pytest.mark.parametrize('change',['valid','packet','reason','consumed','uncertain','unaccounted_call','still_oversized','extra_call'])
def test_uncalled_packet_proof_is_readonly_and_preserves_every_existing_allowance(tmp_path,monkeypatch,change):
    v,s,b,a,project=packing_fixture(tmp_path);us=g.units(v);packet=g.next_packet(v,us,s,project)
    root=tmp_path/'_system/ledgers/ingest-workflows/fixture'
    (root/'output-attempts').mkdir(parents=True);(root/'semantic-calls').mkdir()
    entry={'uncalled_packet':True,'uncalled_packet_sha256':fingerprint(target_view(packet['view']))}
    failure={'error_code':'READING_WINDOW_OVERSIZE',
        'reason':'READING_WINDOW_OVERSIZE at $: complete short-target routing exceeds bound; no truncation or call'}
    if change=='packet':entry['uncalled_packet_sha256']='changed'
    if change=='reason':failure['reason']='indivisible complete original evidence too large'
    if change=='extra_call':entry['continue_exhausted']=True
    if change=='consumed':
        from semantic_output import path_for
        path_for(a,b,'global-plan',target_view(packet['view'])).write_text(json.dumps({'attempts':[]}))
    if change=='uncertain':(root/'output-attempts/unknown.json').write_text(json.dumps({'attempts':[{'task_id':b['task_id'],'state':'reserved','session_id':'unknown-native'}]}))
    if change=='unaccounted_call':(root/'semantic-calls/unknown.json').write_text(json.dumps({'native_task_id':b['task_id']}))
    if change=='still_oversized':
        normal=project
        def project(*args):return {**normal(*args),'input_size':31000 if args[1].get('output_contract') else 29000}
    monkeypatch.setattr(fixed_semantic,'prepare_request_projection',project)
    before={p:p.read_bytes() for p in root.rglob('*.json')}
    if change=='valid':
        result=recovery.uncalled_packet(a,b,v,s,us,packet,entry,failure,set(),[])
        assert result['corrected_proposal_refs']==['p1'] and result['additional_calls_granted']==0
        assert result['existing_attempts_changed'] is False and result['corrected_codepoints']==20000
    else:
        with pytest.raises(ContractError):recovery.uncalled_packet(a,b,v,s,us,packet,entry,failure,set(),[])
    assert before=={p:p.read_bytes() for p in root.rglob('*.json')}
