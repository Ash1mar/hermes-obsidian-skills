#!/usr/bin/env python3
"""Read-only native body-input replay; no model, dispatch or budget mutation."""
import argparse,json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from saved_body_inputs import load,compare
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vault',required=True,type=Path);p.add_argument('--workflow-id',required=True);p.add_argument('--output',required=True,type=Path);p.add_argument('--generation-only',action='store_true');a=p.parse_args()
 if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT') or a.output.resolve().is_relative_to(a.vault.resolve()):raise ValueError('trusted read-only operator and outside-Vault report required')
 wall=time.monotonic();cpu=time.process_time();items,adapter,audits=load(a.vault,a.workflow_id)
 from hermes_source_units.generation_policy import POLICY
 policy=POLICY if a.generation_only else None
 out={'contract':'saved-independent-body-comparison/v1','workflow_id':a.workflow_id,'native_audit_refs':audits,'plan_order':'saved page paths sorted; exact candidate membership retained','page_semantics_policy':policy,
      'profiles':[compare(items,adapter,n,semantic_policy=policy) for n in ('baseline-30000','pages-40000')],
      'wall_seconds':time.monotonic()-wall,'cpu_seconds':time.process_time()-cpu,'actual_new_model_calls':0,'real_two_page_body_acceptance':'pending_authorization','three_four_page_expansion':'disabled_pending_live_two_page_benefit'}
 a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:v for k,v in out.items() if k not in ('profiles','native_audit_refs')}));print(json.dumps([{k:v for k,v in r.items() if k not in ('groups','held','split_decisions')} for r in out['profiles']]))
if __name__=='__main__':main()
