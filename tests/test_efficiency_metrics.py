"""Isolated telemetry contracts: no native execution, model, Provider or real Vault."""
import json
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/lib'))
import workflow_metrics as metrics
import efficiency_metrics as detail
from hermes_source_units import runtime_telemetry as telemetry


def event(name,start=1,end=2,**fields):
    return {'name':name,'span_id':name+str(start),'started_at':start,'finished_at':end,
        'started_monotonic_ns':int(start*1e9),'finished_monotonic_ns':int(end*1e9),
        'elapsed_ms':(end-start)*1000,'pid':1,'clock_domain':'boot','error':None,**fields}


def service(path):
    w={'workflow_id':'w','revision':4,'batch_id':None,'state':'partial','current_stage':'validating',
        'scope':{},'source_outcomes':[]}
    return NS(vault=path,status=lambda _:w)


def test_child_span_inherits_correlation_not_counters(tmp_path,monkeypatch):
    monkeypatch.setattr(telemetry,'runtime_directory',lambda *a:tmp_path)
    with telemetry.span('outer',vault=tmp_path,workflow_id='w',request_id='q') as outer:
        outer['logical_read_sources']=[{'large':'payload'}]
        with telemetry.span('inner'):pass
    rows=[json.loads(s) for p in tmp_path.glob('*.jsonl') for s in p.read_text().splitlines()]
    inner=next(e for e in rows if e['name']=='inner')
    assert inner['request_id']=='q' and inner['parent_span_id']==outer['span_id']
    assert inner['operation_id']==outer['span_id'] and 'logical_read_sources' not in inner
    assert 'observer_prior_write_totals' in inner


def test_reads_deduplicate_shared_ledger_and_preserve_cost_before_cache_hit():
    a=event('workflow_read_projection',scope='unused',read_accounting_scope_id='scope',
        logical_read_sources=[{'path':'p','origin':'json','reads':2,'bytes':20}],
        content_hash_sources=[{'path':'p','hash_calls':2,'hash_bytes':20,'observed_digests':['hash']}],parses=1)
    b={**a,'span_id':'outer','finished_at':3}
    r=detail.reads([a,b,event('graph_dependency_scan',bytes=30,hash_ms=4),event('graph_reuse_summary',hits=1,lookups=1)])
    assert r['scopes']==1 and r['logical_reads']['bytes']==20
    assert r['scan_cost_including_before_hit']['bytes']==30 and r['graph_cache']['hits']==1
    assert r['repeated_accesses'][0]['observed_digests']==['hash']
    assert 'unproven' in r['repeated_accesses'][0]['redundancy']


def test_ack_timeout_is_not_native_completion_and_clock_domains_do_not_subtract():
    rows=[event('worker_registered',0,0,task_id='t',native_run_id=1),
        event('domain_completion_receipt',1,2,task_id='t',native_run_id=1),
        event('completion_acknowledgement',3,123,task_id='t',native_run_id=1,outcome='pending_timeout')]
    r=detail.coordination(rows,metrics.distribution)
    assert r['acknowledgement_outcomes_ms']['pending_timeout']['n']==1
    assert r['release_to_next_worker']==[] and r['chains'][0]['receipt_to_ack_start_ms']==1000
    assert detail.delta(rows[1],{**rows[2],'clock_domain':'other'}) is None
    assert detail.delta(rows[2],rows[1]) is None


def test_same_session_multiple_requests_pair_by_request_identity():
    rows=[event('sdk_request_start',0,0,request_id='a',session_id='s'),
        event('sdk_model_request',0,2,request_id='a',session_id='s'),
        event('sdk_request_start',3,3,request_id='b',session_id='s'),
        event('sdk_model_request',3,4,request_id='b',session_id='s')]
    assert metrics.window(rows,0,5,'w')['sdk']['paired_intervals']==2


def test_call_to_many_receipts_and_unknown_call_not_dropped():
    c={'evidence_ref':'call.json','phase':'page-write','timeline_span_id':'m',
       'input_sha256':'h','schema_sha256':'s','receipt_refs':['one','two'],'model':{'session_id':'session'}}
    r=detail.calls_report([c], [event('model_call',span_id='m'),event('model_call',3,4,error='Stopped')],
        [{'evidence_ref':'attempt.json','attempts':[{'audit_ref':'call.json','attempt':2,'state':'rejected'}]}])
    assert len(r['rows'])==2 and r['rows'][0]['receipt_refs']==['one','two']
    assert r['rows'][0]['durable_attempt']['ordinal']==2
    assert r['rows'][1]['domain_validation']=='unknown'


def test_stage_windows_do_not_reopen_same_stage_on_every_revision():
    rows=[event('workflow_state_committed',i,i,stage=s,state=s,revision=i) for i,s in
        [(1,'preparing'),(2,'preparing'),(3,'analyzing'),(4,'partial')]]
    windows=detail.stages(rows)
    assert len(windows)==3 and windows[0]['start']==1 and windows[0]['cutoff']==3
    assert windows[-1]['cutoff'] is None


def test_collector_reuses_receipts_and_reads_logs_once(tmp_path,monkeypatch):
    root=tmp_path/'_system/ledgers/ingest-workflows/w/semantic-calls';root.mkdir(parents=True)
    receipt=tmp_path/'receipt.json';receipt.write_text('{"ok":true,"validated":true}')
    for n in (1,2):
        (root/f'{n}.json').write_text(json.dumps({'phase':'citation','input_sha256':str(n),'receipt_refs':['receipt.json']}))
    timeline=tmp_path/'timeline';timeline.mkdir()
    line={**event('example'),'vault':str(tmp_path),'workflow_id':'w'}
    (timeline/'events.jsonl').write_text(json.dumps(line)+'\n'+json.dumps(line)+'\n')
    original=Path.read_bytes;count=[]
    def read(p):count.append(p);return original(p)
    monkeypatch.setattr(Path,'read_bytes',read)
    r=metrics.collect(service(tmp_path),'w',timeline=timeline)
    assert count.count(receipt)==1 and len(r['efficiency']['domain_receipts'])==1
    assert r['telemetry_diagnostics']['duplicate_rows_ignored']==1
    assert r['observer']['direct_evidence_reads']['files']==4
    assert r['efficiency']['operator_observations']['status']=='unknown'


def test_external_observations_reject_other_workflow_and_conflicts(tmp_path):
    row={'event_id':'a','kind':'controller_reentry','vault':str(tmp_path),'workflow_id':'other'}
    with pytest.raises(ValueError):metrics.collect(service(tmp_path),'w',timeline=tmp_path,observations=[row])
    row['workflow_id']='w'
    with pytest.raises(ValueError):metrics.collect(service(tmp_path),'w',timeline=tmp_path,observations=[row,{**row,'kind':'maintenance'}])


def test_writer_cumulative_counts_are_not_added_twice():
    rows=[event('e',i,i,observer_prior_write_totals={'events':i,'bytes':i*10,'elapsed_ns':i*100,'failures':0}) for i in (1,2)]
    assert detail.writer_cost(rows)['prior_totals']['bytes']==20


def test_wire_composition_preserves_exact_prompt_and_has_no_model_reads(monkeypatch):
    import fixed_semantic as fixed
    monkeypatch.setattr(fixed,'system_prompt',lambda *a:'规则')
    counts={};view={'a':'中文'};schema={'type':'object'}
    result=fixed.wire_prompt('resource-reduce',view,schema,metrics=counts)
    expected={'phase':'resource-reduce','output_schema':schema,'input':view}
    assert result['user']=='Semantic input v1\n'+json.dumps(expected,ensure_ascii=False,separators=(',',':'))
    assert counts['user']['codepoints']==sum(x['codepoints'] for x in counts['sections'].values())+counts['user_framing_codepoints']
    assert counts['user']['utf8_bytes']==len(result['user'].encode())


def test_final_export_failure_never_retries_domain(tmp_path,monkeypatch):
    import final_metrics
    seen=[]
    monkeypatch.setattr(final_metrics,'runtime_directory',lambda *a:tmp_path)
    monkeypatch.setattr(final_metrics,'event',lambda name,**kw:seen.append(kw))
    monkeypatch.setattr(metrics,'collect',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('collector fault')))
    final_metrics.export(NS(workflow=service(tmp_path)),'w')
    assert seen[-1]['outcome']=='failed' and not (tmp_path/'final-metrics-index.json').exists()


def test_protected_roots_report_added_files(tmp_path):
    protected=tmp_path/'protected';protected.mkdir();(protected/'new').write_text('x')
    r=metrics.collect(service(tmp_path),'w',timeline=tmp_path,protected={},protected_roots=[protected])
    assert r['registry']['W07']['value']['unlisted_new_files']==[str(protected/'new')]


def test_failed_request_without_audit_preserves_usage_session(tmp_path):
    row={**event('sdk_request_start',session_id='lost-session'),'vault':str(tmp_path),'workflow_id':'w'}
    (tmp_path/'events.jsonl').write_text(json.dumps(row)+'\n')
    r=metrics.collect(service(tmp_path),'w',timeline=tmp_path)
    assert r['usage']['unique_sessions']==1 and r['usage']['unknown_samples']['input']==1


def test_conflicting_event_identity_is_quarantined(tmp_path):
    row={**event('sdk_request_start'),'vault':str(tmp_path),'workflow_id':'w'}
    (tmp_path/'events.jsonl').write_text('\n'.join(json.dumps(x) for x in [row,{**row,'phase':'different'},row]))
    rows,diagnostics=metrics.events_from(tmp_path,tmp_path,'w')
    assert rows==[] and diagnostics['conflicting_span_ids']==[row['span_id']]


def test_attempt_yield_does_not_count_unknown_as_success():
    result=detail.attempt_yield([{'attempts':[{'state':'reserved'}]},
        {'attempts':[{'state':'rejected'},{'state':'valid'}]}])
    assert result['first_valid_fraction']==0 and result['first_unknown_outputs']==1
    assert result['additional_attempts']==result['additional_valid_outputs']==1


def test_final_export_once_after_exact_settlement(tmp_path,monkeypatch):
    import final_metrics
    monkeypatch.setattr(final_metrics,'runtime_directory',lambda *a:tmp_path)
    monkeypatch.setattr(final_metrics,'event',lambda *a,**kw:None)
    result={'snapshot_consistent':True,'current_snapshot':{'revision':4},'efficiency':{'native_settlement':
        {'state':'settled','workflow_id':'w','vault':str(tmp_path),'revision':4}}}
    calls=[]
    def collect(*a,**k):calls.append(1);return result
    monkeypatch.setattr(metrics,'collect',collect)
    adapter=NS(workflow=service(tmp_path))
    final_metrics.export(adapter,'w');final_metrics.export(adapter,'w')
    assert len(calls)==1 and json.loads((tmp_path/'final-metrics-4.json').read_text())==result
