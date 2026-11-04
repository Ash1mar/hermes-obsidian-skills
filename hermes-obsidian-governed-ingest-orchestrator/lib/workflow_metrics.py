"""Read-only metrics: current authority and historical samples have separate scopes.

Telemetry is observational, never proof of quality, completion or zero failures.
Nested/concurrent spans are distributions, not additive workflow wall time.
"""
from collections import Counter, defaultdict
import json
import hashlib
import math
from pathlib import Path
import sqlite3
import time

from hermes_source_units.runtime_telemetry import runtime_directory
from hermes_source_units.validation import fingerprint


def distribution(values):
    values = sorted(values)
    if not values:
        return {'status': 'no_sample', 'n': 0}
    def percentile(q):
        pos = (len(values)-1)*q
        lo = int(pos)
        return values[lo]+(values[min(lo+1,len(values)-1)]-values[lo])*(pos-lo)
    return {'status':'measured','n':len(values),'mean':sum(values)/len(values),
            'p50':percentile(.5),'p90':percentile(.9),'max':max(values),
            'quantiles':'linear interpolation',**({'samples':values} if len(values)<5 else {})}


def events_from(root, vault, workflow_id):
    events = {}; invalid = duplicates = 0
    for path in sorted(Path(root).glob('*.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
                if row.get('workflow_id') != workflow_id or Path(row.get('vault','')).resolve()!=Path(vault).resolve():
                    continue
                if not row.get('span_id') or not isinstance(row.get('name'),str):
                    raise ValueError('missing event identity')
                for key in ('started_at','finished_at','elapsed_ms'):
                    if not isinstance(row.get(key),(int,float)) or not math.isfinite(row[key]):
                        raise ValueError('invalid time')
                if row['elapsed_ms']<0 or row['finished_at']<row['started_at']:
                    raise ValueError('invalid interval')
                if row['span_id'] in events:
                    if events[row['span_id']]!=row:raise ValueError('conflicting event identity')
                    duplicates+=1
                else:events[row['span_id']]=row
            except (ValueError,TypeError,KeyError):
                invalid+=1
    return list(events.values()), {'invalid_rows':invalid,'duplicate_rows_ignored':duplicates}


def window(events, lower, upper, label):
    if not (math.isfinite(lower) and math.isfinite(upper) and upper>lower):
        raise ValueError('window requires finite start < cutoff')
    completed=[e for e in events if lower<=e['finished_at']<upper]
    grouped=defaultdict(list)
    for e in completed:grouped[e['name']].append(e)
    spans={name:{'successful_ms':distribution([e['elapsed_ms'] for e in rows if not e.get('error')]),
        'failed_ms':distribution([e['elapsed_ms'] for e in rows if e.get('error')]),
        'errors':dict(Counter(e['error'] for e in rows if e.get('error'))),
        'sample_span_ids':[e['span_id'] for e in rows]} for name,rows in sorted(grouped.items())}
    # Pair exact sessions; ambiguous repeated starts are unknown, not fabricated.
    starts=defaultdict(list);ends=defaultdict(list)
    for e in events:
        key=(e.get('session_id'),e.get('phase'),e.get('pid'),e.get('clock_domain'))
        if e['name']=='sdk_request_start':starts[key].append(e)
        if e['name']=='sdk_model_request':ends[key].append(e)
    intervals=[];unknown=0
    for key in starts.keys()|ends.keys():
        a,b=starts[key],ends[key]
        if not key[0] or len(a)!=1 or len(b)!=1:
            if any(lower<=r['started_at']<upper for r in a+b):unknown+=1
            continue
        start,end=a[0]['started_at'],b[0]['finished_at']
        if not key[3] or end<start:
            unknown+=1;continue
        if start<upper and end>lower:intervals.append((max(lower,start),min(upper,end)))
    points=sorted([(a,1) for a,b in intervals]+[(b,-1) for a,b in intervals])
    level=peak=area=busy=0;previous=lower
    for stamp,delta in points:
        area+=(stamp-previous)*level;busy+=(stamp-previous)*bool(level)
        level+=delta;peak=max(peak,level);previous=stamp
    sdk={'status':'measured' if intervals else 'no_sample','paired_intervals':len(intervals),
         'unknown_pairs':unknown,'scope':'half-open fixed wall-clock window; observed paired requests only'}
    if intervals:sdk.update(peak=peak,average=area/(upper-lower),zero_inflight_fraction=1-busy/(upper-lower))
    return {'window_id':label,'start':lower,'cutoff':upper,'seconds':upper-lower,
            'attribution':'completed spans by finish; SDK intervals clipped to window',
            'program_span_ms':spans,'sdk':sdk,
            'cross_cutoff_spans':sum(e['started_at']<upper<=e['finished_at'] for e in events),
            'clock_domains':sorted({e.get('clock_domain','unknown') for e in completed}),
            'wall_time_caveat':'wall clock locates windows; elapsed_ms is producer monotonic duration; no cross-domain subtraction'}


def usage_for(calls, database):
    ids=sorted({c['model']['session_id'] for c in calls if c.get('model',{}).get('session_id')})
    rows=[]
    if database is not None:
        with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
            for sid in ids:
                row=db.execute('SELECT input_tokens,output_tokens,cache_read_tokens,reasoning_tokens,api_call_count FROM sessions WHERE id=?',(sid,)).fetchone()
                rows.append({'session_id':sid,**dict(zip(('input','output','cache_read_subset','reasoning','api_calls'),row))} if row else {'session_id':sid})
    else:rows=[{'session_id':sid} for sid in ids]
    names=('input','output','cache_read_subset','reasoning','api_calls')
    return {'unique_sessions':len(ids),'by_session':rows,
        'known_totals':{k:sum(r[k] for r in rows if isinstance(r.get(k),(int,float))) for k in names},
        'unknown_samples':{k:sum(not isinstance(r.get(k),(int,float)) for r in rows) for k in names},
        'cost_currency':'unknown','scope':'whole-workflow recorded sessions, including recorded failures; cache is an input subset'}


def collect(service, workflow_id, *, timeline=None, database=None, windows=(), protected=None):
    started=time.monotonic();w=service.status(workflow_id);vault=service.vault
    root=vault/f'_system/ledgers/ingest-workflows/{workflow_id}'
    events,diagnostics=events_from(timeline or runtime_directory(vault,workflow_id),vault,workflow_id)
    calls={}
    for p in sorted((root/'semantic-calls').glob('*.json')):
        row=json.loads(p.read_text(encoding='utf-8'));calls[fingerprint(row)]=row
    calls=list(calls.values())
    snapshot={k:w.get(k) for k in ('workflow_id','batch_id','revision','state','current_stage','release_id','release_plan_id','cancel_requested','pause_control')}
    snapshot['scope_digest']=fingerprint(w['scope'])
    snapshot['execution_policy']=w['scope'].get('execution_policy',{'status':'legacy_unpinned'})
    snapshot['recovery_policy']=w['scope'].get('recovery_policy',{'status':'legacy_unpinned'})
    snapshot['source_states']=dict(Counter(s['status'] for s in w.get('source_outcomes',[])))
    snapshot['native_settlement']='unknown; domain terminal state alone does not prove native drain'
    batch=service.knowledge._batch(w['batch_id']) if w.get('batch_id') else None
    if batch:
        slices=service.knowledge._slices(w['batch_id'])
        snapshot['plan']={'tasks':len(batch['task_ids']),'slices':len(slices),'slice_states':dict(Counter(s['state'] for s in slices))}
    chosen=list(windows)
    if events:
        chosen.insert(0,{'start':min(e['started_at'] for e in events),
            'cutoff':max(e['finished_at'] for e in events)+.000001,'window_id':'historical_including_pauses_and_maintenance'})
    history=[window(events,float(x['start']),float(x['cutoff']),x['window_id']) for x in chosen]
    usage=usage_for(calls,database)
    definitions=json.loads((Path(__file__).resolve().parents[1]/'config/metric-definitions.json').read_text(encoding='utf-8'))
    registry={key:{'definition':definition,'status':'unknown','reason':'no authoritative collector for this metric'} for key,definition in definitions.items()}
    def measured(key,value,scope,evidence):
        registry[key].update(status='measured',value=value,scope=scope,evidence=evidence)
        registry[key].pop('reason',None)
    measured('S04',snapshot,'current_snapshot','workflow ledger at recorded revision')
    measured('A01',snapshot['source_states'],'current_snapshot','source_outcomes')
    if batch:
        measured('W02',snapshot['plan'],'current_snapshot','current batch and slices')
        measured('W04',snapshot['plan']['slice_states'],'current_snapshot; slices only, not individual Pass records','current slices')
    if calls:measured('M01',dict(Counter(c['phase'] for c in calls)),'historical recorded call files','semantic-calls/*.json; content deduplicated')
    if usage['unique_sessions']:measured('C01',usage,'historical recorded sessions; partial when unknown_samples > 0','native state.db read-only')
    mapping={'M07':('sdk_model_request','semantic_call','request_construct'),
             'P03':('full_reconciliation',),'P04':('native_cli',),
             'P10':('pass_total','atomic_pass','completion_acknowledgement')}
    for key,names in mapping.items():
        rows=[{'window_id':h['window_id'],'components':{n:h['program_span_ms'][n] for n in names if n in h['program_span_ms']}} for h in history]
        rows=[r for r in rows if r['components']]
        if rows:measured(key,rows,'historical windows; components not additive end-to-end latency','telemetry span IDs')
    if history:measured('M09',[{'window_id':h['window_id'],**h['sdk']} for h in history],'historical paired requests; missing pairs explicitly unknown','telemetry')
    if w['state'] in ('completed','partial'):measured('Z01',snapshot,'current domain terminal state; native settlement separate','workflow ledger')
    if w.get('release_id'):
        release_path=vault/f"_system/knowledge-releases/{w['release_id']}/manifest.json"
        if release_path.is_file():
            raw=release_path.read_bytes();release=json.loads(raw)
            release_hash=hashlib.sha256(raw).hexdigest()
            delivery=release.get('delivery',{})
            measured('L02',{'release_id':w['release_id'],'release_hash':release_hash,'delivery':delivery},
                'current recorded release; publication evidence, not new validation',str(release_path))
            pp=vault/'_system/reports/retrieval-index-manifest.json'
            if pp.is_file():
                provider=json.loads(pp.read_text(encoding='utf-8'))
                matching=provider.get('release_id')==w['release_id'] and provider.get('release_hash')==release_hash
                view={k:provider.get(k) for k in ('status','release_id','release_hash','index_generation','projection_counts','sync_metrics')}
                view.update(matches_current_release=matching,actual_host_index_checked=False)
                measured('I01',view,'current portable Provider record; host index not probed',str(pp))
                if matching and provider.get('sync_metrics'):
                    measured('I02',provider['sync_metrics'].get('seconds',{}),'recorded sync for exact current release',str(pp))
                    measured('I04',{k:v for k,v in provider['sync_metrics'].items() if k.endswith('_documents')},'recorded sync for exact current release',str(pp))
    if protected is not None:
        changed=[];missing=[]
        for name,expected in protected.items():
            path=Path(name)
            if not path.is_absolute() or not isinstance(expected,str) or len(expected)!=64:raise ValueError('invalid protected hash baseline')
            if not path.is_file():missing.append(name)
            elif hashlib.sha256(path.read_bytes()).hexdigest()!=expected:changed.append(name)
        measured('W07',{'baseline_files':len(protected),'unchanged':len(protected)-len(changed)-len(missing),
            'changed':changed,'missing':missing,'unlisted_new_files':'not_checked'},'explicit protected baseline only','provided baseline hashes and current bytes')
    after=service.status(workflow_id)
    return {'contract':'workflow-metrics/v1','collected_at':time.time(),'vault':str(vault),
        'current_snapshot':snapshot,'snapshot_consistent':after['revision']==w['revision'],
        'revision_after':after['revision'],'historical_windows':history,'usage':usage,
        'registry':registry,'telemetry_diagnostics':diagnostics,
        'observer':{'elapsed_seconds':time.monotonic()-started,'model_calls':0,'domain_mutations':0},
        'limitations':['Telemetry may be incomplete; missing samples are not zero.',
            'Historic whole-span concurrency includes maintenance; supply explicit windows for comparisons.',
            'Quality, hash preservation, Provider readiness and query accuracy require separate evidence.']}
