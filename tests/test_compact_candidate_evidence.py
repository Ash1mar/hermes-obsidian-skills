"""Isolated downstream original-evidence checks, not native domain acceptance."""
import copy
import hashlib
import json

import pytest
from test_compact_execution import vault
from test_p3_knowledge_build import prepare_layered_reduce_batch
from test_phase_grouping_dependencies import dependency_fixture
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.reading_dependencies import required_reading
import fixed_evidence
import fixed_semantic as fixed


def inventory(vault):
    return {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in vault.rglob('*') if p.is_file()}


def test_original_evidence_retains_old_records_source_context_and_closed_handles(vault):
    service,refs,ids,_=prepare_layered_reduce_batch(vault,'compact-evidence')
    index,_=service._pass_index(ids)
    pairs=[pair for pair in index.values() if pair[0]['pass_kind']=='citation']
    before=inventory(vault)
    packet,readings=fixed_evidence.original_evidence(service,[r for r,c in pairs],'agent',1)
    assert len(packet['tasks'])==2
    assert all(not {'input_id','next_sequence','continuation'} & set(t) for t in packet['tasks'])
    assert any('requires filtered fluid' in text for text in packet['texts'].values())
    links=[fixed_evidence.support(service,r,c,readings) for r,c in pairs]
    assert len({link['reading'] for link in links})==2
    selected=fixed_evidence.selected(packet,{links[0]['reading']})
    assert len(selected['tasks'])==1
    assert len(selected['texts'])<len(packet['texts'])
    assert all(m.get('text_ref') in selected['texts'] for m in selected['tasks'][0]['materials'])
    assert inventory(vault)==before
    with pytest.raises(ContractError,match='STALE_INPUT'):
        fixed_evidence.original_evidence(service,[pairs[0][0]],'different-actor',1)
    invalid=copy.deepcopy(pairs[0][1]);invalid['support_refs']=[refs[1]]
    with pytest.raises(ContractError,match='OUTSIDE_READING_PACKAGE'):
        fixed_evidence.support(service,pairs[0][0],invalid,readings)
    path=vault/f'_system/knowledge-builds/task-{pairs[0][0]["task_id"]}/readings/{pairs[0][0]["reading_package_id"]}.json'
    path.write_text('{}')
    with pytest.raises((ContractError,KeyError)):
        fixed_evidence.original_evidence(service,[pairs[0][0]],'agent',1)


def test_original_required_table_is_supplied_and_uninspected_history_held(vault):
    service,package=dependency_fixture(vault)
    path=vault/f'_system/knowledge-builds/task-dependency-task/readings/{package["package_id"]}.json'
    path.write_text(json.dumps(package))
    core=package['window']['core_refs'];_,supplement=required_reading(service.source,package)
    record={'contract':'hermes-knowledge-pass/v1','task_id':'dependency-task',
        'task_revision':service._task('dependency-task')['revision'],'actor':'agent',
        'pass_kind':'citation','sequence':1,'reading_window_id':package['window']['window_id'],
        'reading_package_id':package['package_id'],'inspections':[
            {'source_ref':r,'finding':'isolated inspected','qa':'usable','qa_note':''} for r in core],
        'candidates':[],'empty_reason':'isolated transport fixture'}
    record['pass_id']=fingerprint(record)
    before=inventory(vault)
    with pytest.raises(ContractError,match='INCOMPLETE_COVERAGE'):
        fixed_evidence.original_evidence(service,[record],'agent',1)
    record['inspections'].extend({'source_ref':r,'finding':'isolated table inspected','qa':'qa_required',
        'qa_note':'Keep source limitation'} for r in supplement['required_refs'])
    record['pass_id']=fingerprint({k:v for k,v in record.items() if k!='pass_id'})
    packet,_=fixed_evidence.original_evidence(service,[record],'agent',1)
    required=[m for t in packet['tasks'] for m in t['materials'] if m.get('required_dependency')]
    assert len(required)==1 and '2 h' in packet['texts'][required[0]['text_ref']]
    assert not any('associated |' in text for text in packet['texts'].values())
    assert inventory(vault)==before
    oversized={'original_evidence':packet,'content':'x'*40000}
    class NeverCalled:
        def __call__(self,*args): raise AssertionError('oversize evidence must not call model')
    with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):
        fixed.model_call(None,{},'page-review',oversized,NeverCalled(),30000)
