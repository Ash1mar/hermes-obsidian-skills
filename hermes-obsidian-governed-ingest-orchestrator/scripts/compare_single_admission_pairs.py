#!/usr/bin/env python3
"""Fixed54-plan style factor isolation: budget constant, QA then admission reuse."""
import argparse,json,os,sys,time
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from saved_body_inputs import load,compare
from hermes_source_units.generation_policy import POLICY
from hermes_source_units.read_accounting import accounting_scope

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vault',required=True,type=Path);p.add_argument('--workflow-id',required=True);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
 if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT') or a.output.resolve().is_relative_to(a.vault.resolve()):raise ValueError('trusted read-only operator and outside-Vault report required')
 wall=time.monotonic();cpu=time.process_time()
 with accounting_scope() as ledger:
  items,adapter,audits=load(a.vault,a.workflow_id)
  stages=[]
  for name,kwargs in [('original_40000',{}),('remove_QA_only',{'pairing_mode':'qa_only'}),
       ('reuse_text_single_admission',{'pairing_mode':'single_admission'}),
       ('future_generation_only_complete_request',{'pairing_mode':'single_admission','future_pairing':True,'semantic_policy':POLICY})]:
   t=time.monotonic();c=time.process_time();r=compare(items,adapter,'pages-40000',**kwargs)
   r.update(stage=name,wall_seconds=time.monotonic()-t,cpu_seconds=time.process_time()-c,
        split_reason_counts=dict(Counter(reason for s in r['split_decisions'] for reason in s['reasons'])),
        solely_QA_split_decisions=sum(s['reasons']==['assigned-candidate-qa-unresolved'] for s in r['split_decisions']))
   stages.append(r)
  asset_rows=[];asset_started=time.monotonic();asset_cpu=time.process_time()
  before_reads=sum(r['reads'] for r in ledger.rows.values());before_bytes=sum(r['bytes'] for r in ledger.rows.values())
  for page,v in items:
   ms=[m for t in v['original_evidence']['tasks'] for m in t['materials']]
   assets=[m['asset'] for m in ms if m.get('asset')];links=[m.get('linked_assets') for m in ms if m.get('linked_assets')]
   asset_rows.append({'page':page['path'],'image_transport_assets':assets,'link_only_materials':len(links),
      'single_decision_reused':not assets,'paired_asset_transport_enabled':False})
  asset_program_wall=time.monotonic()-asset_started;asset_program_cpu=time.process_time()-asset_cpu
  asset_reads=sum(r['reads'] for r in ledger.rows.values())-before_reads;asset_bytes=sum(r['bytes'] for r in ledger.rows.values())-before_bytes
  reads={'logical_reads':sum(r['reads'] for r in ledger.rows.values()),'logical_bytes':sum(r['bytes'] for r in ledger.rows.values()),
     'scope':'instrumented file reads for authentication and comparisons; SQLite internal reads not measured'}
 assert all(r['same_planned_pages']==len(items) for r in stages)
 out={'contract':'single-admission-pair-comparison/v1','workflow_id':a.workflow_id,'same_page_plan':len(items),'native_audit_refs':audits,
    'stages':stages,'assets':{'pages_with_actual_image_transport':sum(bool(r['image_transport_assets']) for r in asset_rows),
       'pages_with_links_but_no_image_transport':sum(bool(r['link_only_materials']) and not r['image_transport_assets'] for r in asset_rows),
       'rows':asset_rows,'real_asset_pair_acceptance':'not_covered_without_actual_image_pair',
       'pairing_asset_file_reads':0,'pairing_asset_file_bytes':0,'qualification_reuses_only_memory_and_existing_single_decisions':True},
    'asset_inventory_program_cost':{'logical_file_reads':asset_reads,'logical_bytes':asset_bytes,'wall_seconds':asset_program_wall,'cpu_seconds':asset_program_cpu,
       'scope':'existing in-memory registered material/transport records; original authentication and request construction are reported separately'},
    'read_metrics':reads,'wall_seconds':time.monotonic()-wall,'cpu_seconds':time.process_time()-cpu,
    'actual_new_model_calls':0,'actual_new_token_usage':None,'model_wait_seconds':None,'real_quality_and_concurrent_single_wall_comparison':'pending_explicit_authorization',
    'scope':'Counterfactual pre-outcome same plan only. Existing completed/deferred pages are never redispatched. QA/asset tags and publication gates are unchanged.'}
 a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps([{k:r[k] for k in ('stage','estimated_requests','pairs','covered_pages','held_pages','solely_QA_split_decisions','split_reason_counts','wall_seconds','cpu_seconds')} for r in stages]))
 print(json.dumps({k:v for k,v in out.items() if k not in ('stages','native_audit_refs','assets')}))
if __name__=='__main__':main()
