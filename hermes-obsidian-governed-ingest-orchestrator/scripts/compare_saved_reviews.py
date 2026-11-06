#!/usr/bin/env python3
"""Read-only authenticated review packing comparison; never a native call."""
import argparse,json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from saved_review_inputs import load,compare

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vault',required=True,type=Path)
    p.add_argument('--workflow-id',required=True);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--hermes-home',type=Path);a=p.parse_args()
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise ValueError('comparison requires a trusted read-only operator')
    if a.output.resolve().is_relative_to(a.vault.resolve()):raise ValueError('report must stay outside the real Vault')
    started=time.monotonic();cpu=time.process_time();rows,adapter=load(a.vault,a.workflow_id,home=a.hermes_home)
    from collections import Counter
    out={'contract':'saved-review-packing-comparison/v1','workflow_id':a.workflow_id,
        'authenticated_native_inputs':len(rows),'risk_counts':dict(Counter(risk for r in rows for risk in r['assessment']['risk_reasons'])),
        'historical_usage':{k:sum(r['usage'][k] or 0 for r in rows) for k in ('input','output','api_calls')},
        'historical_substantive_opinions':sum(r['response'].get('decision')=='revision_required' for r in rows),
        'profiles':[compare(rows,adapter,name) for name in ('baseline-30000','review-40000')],
        'mechanical_wall_seconds':time.monotonic()-started,'mechanical_cpu_seconds':time.process_time()-cpu,
        'actual_new_model_calls':0,'actual_new_token_usage':None,'model_wait_seconds':None,'maintenance_window':'reported separately by operator'}
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in out.items() if k!='profiles'}));print(json.dumps([
        {k:v for k,v in row.items() if k not in ('request_groups','held')} for row in out['profiles']]))
if __name__=='__main__':main()
