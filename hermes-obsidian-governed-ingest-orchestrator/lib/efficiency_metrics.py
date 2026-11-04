"""Pure aggregation of existing evidence. No workflow calls, scans or model access."""
from collections import Counter, defaultdict


def numeric_totals(rows, keys):
    return {k:sum(r.get(k,0) for r in rows if isinstance(r.get(k,0),(int,float))) for k in keys}


def reads(events):
    # A ledger may be emitted by nested scopes. Select its final largest snapshot
    # once; legacy projections are independent only when no ancestor owns it.
    by_id={e['span_id']:e for e in events};selected={};legacy=[]
    for e in events:
        if e.get('name')!='workflow_read_projection':continue
        scope=e.get('read_accounting_scope_id')
        if scope:
            key=(e.get('clock_domain'),e.get('pid'),scope)
            old=selected.get(key)
            if old is None or e['finished_at']>old['finished_at']:selected[key]=e
        else:
            parent=by_id.get(e.get('parent_span_id'));seen=set();nested=False
            while parent and parent['span_id'] not in seen:
                seen.add(parent['span_id'])
                if parent.get('name')=='workflow_read_projection':nested=True;break
                parent=by_id.get(parent.get('parent_span_id'))
            if not nested:legacy.append(e)
    scopes=list(selected.values())+legacy
    rows=[r for e in scopes for r in e.get('logical_read_sources',[])]
    hashes=[r for e in scopes for r in e.get('content_hash_sources',[])]
    hot=defaultdict(Counter)
    for r in rows:
        hot[(r['path'],r['origin'])].update({k:r.get(k,0) for k in ('reads','bytes','captures','failures')})
    duplicates=[]
    for e in scopes:
        for r in e.get('logical_read_sources',[]):
            if r.get('reads',0)>1:
                versions={d for h in e.get('content_hash_sources',[]) if h['path']==r['path'] for d in h.get('observed_digests',[])}
                duplicates.append({'operation_id':e.get('operation_id',e['span_id']),'path':r['path'],
                    'origin':r['origin'],'reads':r['reads'],'bytes':r.get('bytes',0),
                    'observed_digests':sorted(versions),'redundancy':'unproven; guards and operation boundary must be checked'})
    graphs=[e for e in events if e['name']=='graph_reuse_summary']
    scans=[e for e in events if e['name']=='graph_dependency_scan']
    return {'status':'measured' if scopes else 'no_sample','scopes':len(scopes),
        'logical_reads':numeric_totals(rows,('reads','bytes','captures','failures')),
        'unique_paths':len({r['path'] for r in rows}),
        'content_hash':numeric_totals(hashes,('hash_calls','hash_bytes','hash_ns','digest_reuses')),
        'parse_cache':numeric_totals(scopes,('parses','validations','hits','parse_ns','validation_ns','copy_ns',
            'full_copies','projected_copies','read_hash_ns','workflow_hydrate_ns','canonical_hits','canonical_builds')),
        'graph_cache':numeric_totals(graphs,('lookups','hits','builds','cohorts','fingerprint_calls','fingerprint_ns','proof_copy_ns')),
        'scan_cost_including_before_hit':numeric_totals(scans,('files','bytes','read_ms','hash_ms','directory_enumerations')),
        'hotspots':[{'path':p,'origin':o,**c} for (p,o),c in sorted(hot.items(),key=lambda x:x[1]['bytes'],reverse=True)[:20]],
        'repeated_access_count':len(duplicates),'repeated_accesses':sorted(duplicates,key=lambda r:r['bytes'],reverse=True)[:100],
        'repeated_access_limit':100,
        'scope':'instrumented logical reads only; raw disk I/O and uninstrumented entry points unknown; counters overlap across categories'}


def calls_report(calls, events, attempts):
    models={e['span_id']:e for e in events if e['name']=='model_call'}
    attempts_by_audit={a.get('audit_ref'):{'journal_ref':j.get('evidence_ref'),
        'ordinal':a.get('attempt'),'state':a.get('state'),'attempt_id':a.get('attempt_id')}
        for j in attempts for a in j.get('attempts',[]) if a.get('audit_ref')}
    rows=[];seen=set();groups=defaultdict(list)
    for call in calls:
        event=models.get(call.get('timeline_span_id'),{})
        ref=call.get('evidence_ref');attempt=attempts_by_audit.get(ref,{})
        row={k:call.get(k) for k in ('request_id','native_task_id','native_run_id','node','phase','input_sha256',
            'schema_sha256','logical_action_id','attempt_ordinal','call_category','correction_reason',
            'task_identities','subject_refs','receipt_refs','validated','error_code','input_composition','output_attempt_id',
            'output_codepoints','output_utf8_bytes')}
        row.update(audit_ref=ref,session_id=call.get('model',{}).get('session_id'),
            span_id=call.get('timeline_span_id'),started_at=event.get('started_at'),finished_at=event.get('finished_at'),
            durable_attempt=attempt,domain_validation='unknown' if call.get('validated') is None else call['validated'])
        rows.append(row);seen.add(call.get('timeline_span_id'))
        groups[(call.get('node'),call.get('phase'),call.get('input_sha256'),call.get('schema_sha256'))].append(ref)
    for key,e in models.items():
        if key not in seen:
            rows.append({'span_id':key,'request_id':e.get('request_id'),'native_task_id':e.get('task_id'),
                'phase':e.get('phase'),'input_sha256':e.get('input_sha256'),'error_code':e.get('error'),
                'audit_ref':None,'domain_validation':'unknown','finished_at':e['finished_at']})
    sdk=[e for e in events if e['name']=='sdk_model_request'];starts=[e for e in events if e['name']=='sdk_request_start']
    sessions={r.get('session_id') for r in rows if r.get('session_id')}
    request_ids={r.get('request_id') for r in rows if r.get('request_id')}
    actual={}
    for e in sdk:
        identity=e.get('request_id')
        if identity:actual.setdefault(identity,[]).append(e)
    for e in starts:
        if e.get('request_id'):actual.setdefault(e['request_id'],[])
    requests=[{'request_id':key,'phase':(v[0].get('phase') if v else next((e.get('phase') for e in starts if e.get('request_id')==key),None)),
        'outcome':('unknown' if len(v)!=1 or any(e.get('error') for e in v) else 'returned'),
        'finish_span_ids':[e['span_id'] for e in v]} for key,v in actual.items()]
    audit_sessions=Counter(r.get('session_id') for r in rows if r.get('session_id'))
    for row in rows:
        row['request_accounting']='audit_not_new_request'
        row['shared_session_audits']=audit_sessions.get(row.get('session_id'),0)
    return {'actual_sdk_requests':len(requests),'requests':requests,
        'actual_by_phase':dict(Counter(r.get('phase','unknown') for r in requests)),
        'unknown_requests':sum(r['outcome']=='unknown' for r in requests),
        'unidentified_sdk_finish_span_ids':[e['span_id'] for e in sdk if not e.get('request_id')],
        'audit_rows':len(calls),'transport_accounting':'nested entry events are not physical HTTP requests',
        'rows':rows,'by_phase':dict(Counter(r.get('phase','unknown') for r in requests)),
        'audit_by_phase':dict(Counter(c.get('phase','unknown') for c in calls)),
        'by_category':dict(Counter(c.get('call_category') or 'unknown' for c in calls)),
        'sdk_starts':len(starts),'sdk_finishes':len(sdk),
        'sdk_unlinked_span_ids':[e['span_id'] for e in sdk if e.get('request_id') not in request_ids and e.get('session_id') not in sessions],
        'transport_entries':[{k:e.get(k) for k in ('span_id','request_id','session_id','request_sha256','started_at')} for e in events if e['name']=='sdk_transport_entry'],
        'reused_responses':[e for e in events if e['name']=='semantic_response_reused'],
        'same_input_candidates':[{'node':k[0],'phase':k[1],'input_sha256':k[2],'schema_sha256':k[3],
            'audits':refs,'redundancy':'unproven; compare attempts, authority and receipts'} for k,refs in groups.items() if len(refs)>1],
        'usage_scope':'session usage is not divided between requests; unknown physical HTTP retries stay unknown'}


def attempt_yield(journals):
    known=valid=additional=additional_valid=unknown=0
    for journal in journals:
        attempts=journal.get('attempts',[])
        if not attempts:continue
        first=attempts[0].get('state')
        if first in ('valid','rejected'):
            known+=1;valid+=int(first=='valid')
        else:unknown+=1
        for row in attempts[1:]:
            additional+=1;additional_valid+=int(row.get('state')=='valid')
    return {'first_known_outputs':known,'first_valid_outputs':valid,'first_unknown_outputs':unknown,
        'first_valid_fraction':valid/known if known else None,'additional_attempts':additional,
        'additional_valid_outputs':additional_valid,
        'scope':'durable output-validation journals only; not full domain acceptance or factual accuracy'}


def delta(a,b):
    if not a or not b:return None
    x=a.get('finished_monotonic_ns');y=b.get('started_monotonic_ns')
    if not a.get('clock_domain') or a.get('clock_domain')!=b.get('clock_domain') or x is None or y is None or y<x:return None
    return (y-x)/1e6


def coordination(events, distribution):
    tasks=defaultdict(list)
    for e in events:
        if e.get('task_id'):tasks[str(e['task_id'])].append(e)
    chains=[]
    for task,rows in tasks.items():
        workers=[e for e in rows if e['name']=='worker_registered']
        for worker in workers:
            run=worker.get('native_run_id')
            same=[e for e in rows if str(e.get('native_run_id'))==str(run) and e.get('clock_domain')==worker.get('clock_domain')]
            def one(name):
                found=[e for e in same if e['name']==name]
                return found[0] if len(found)==1 else None
            receipt=one('domain_completion_receipt');ack=one('completion_acknowledgement')
            ready=[e for e in rows if e['name']=='dispatch_ready' and e['started_at']<=worker['started_at']]
            last=max(ready,key=lambda e:e['started_at']) if ready else None
            chains.append({'task_id':task,'native_run_id':run,'node':worker.get('node'),
                'ready_to_worker_ms':delta(last,worker),'receipt_to_ack_start_ms':delta(receipt,ack),
                'ack_wait_ms':ack['elapsed_ms'] if ack else None,'ack_outcome':ack.get('outcome','unknown') if ack else 'unknown',
                'receipt_span_id':receipt['span_id'] if receipt else None,'worker_span_id':worker['span_id'],
                'ack_span_id':ack['span_id'] if ack else None})
    ack=[e for e in events if e['name']=='completion_acknowledgement']
    outcomes={name:distribution([e['elapsed_ms'] for e in ack if e.get('outcome','unknown')==name])
        for name in {e.get('outcome','unknown') for e in ack}}
    # Release-to-next-worker is an observation, NOT proof that spare capacity and
    # eligible work existed throughout that interval.
    releases=[e for e in ack if e.get('outcome')=='native_grant_released']
    workers=sorted([e for e in events if e['name']=='worker_registered'],key=lambda e:e['started_at'])
    handoffs=[]
    for release in releases:
        next_worker=next((e for e in workers if e['started_at']>=release['finished_at'] and
            e.get('node','').partition(':')[0]==release.get('node','').partition(':')[0]),None)
        if next_worker:
            handoffs.append({'from':release['span_id'],'to':next_worker['span_id'],
                'elapsed_ms':delta(release,next_worker),'eligible_idle':'unknown; requires admission evidence'})
    return {'chains':chains,'acknowledgement_outcomes_ms':outcomes,'release_to_next_worker':handoffs,
        'capacity_observations':[{k:e.get(k) for k in ('span_id','task_id','capacity','occupied','ready_unleased','started_at')}
            for e in events if e['name']=='pass_capacity_observed'],
        'cli_operations':dict(Counter(e.get('operation','unknown') for e in events if e['name']=='native_cli')),
        'holds':[{k:e.get(k) for k in ('span_id','task_id','reason','projected_state','started_at')} for e in events if e['name']=='dispatch_hold'],
        'admission_errors':dict(Counter(e.get('error') or 'admitted' for e in events if e['name']=='claim_admission')),
        'native_claim_timing':'use worker_registered and admission bounds; exact DB commit not instrumented',
        'slot_release_timing':'native grant release observation; not exact lease transaction timestamp'}


def stages(events):
    changes=sorted([e for e in events if e['name']=='workflow_state_committed'],key=lambda e:e['started_at'])
    result=[]
    for e in changes:
        key=(e.get('stage'),e.get('state'),e.get('cancel_requested'),e.get('pause_boundary'),e.get('clock_domain'))
        if result and result[-1]['key']==key:continue
        if result:result[-1]['cutoff']=e['started_at']
        result.append({'key':key,'stage':e.get('stage'),'state':e.get('state'),
            'start':e['started_at'],'cutoff':None,'revision':e.get('revision'),'evidence_span_id':e['span_id']})
    return [{'window_id':'stage-'+str(i+1),**{k:v for k,v in r.items() if k!='key'},
        'scope':'observed committed state; may contain waits; separate node/phase spans for Map and Reduce'} for i,r in enumerate(result)]


def writer_cost(events):
    writers={}
    for e in events:
        counts=e.get('observer_prior_write_totals')
        if counts is not None:
            key=(e.get('clock_domain'),e.get('pid'))
            if key not in writers or counts['events']>writers[key]['events']:writers[key]=counts
    return {'process_count':len(writers),'prior_totals':numeric_totals(writers.values(),('events','bytes','elapsed_ns','failures')),
        'scope':'process-wide cumulative prior writes, not workflow-exclusive; final write and post-crash losses unknown'}
