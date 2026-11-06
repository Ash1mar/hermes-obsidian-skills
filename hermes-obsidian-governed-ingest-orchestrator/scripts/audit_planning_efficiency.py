#!/usr/bin/env python3
"""Read-only saved-input planning cost audit; does not start a workflow."""
import argparse,json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from planning_efficiency import audit
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vault',required=True,type=Path);p.add_argument('--workflow-id',required=True);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
 if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT') or a.output.resolve().is_relative_to(a.vault.resolve()):raise ValueError('trusted read-only operator and outside-Vault report required')
 wall=time.monotonic();cpu=time.process_time();out=audit(a.vault,a.workflow_id);out.update(mechanical_wall_seconds=time.monotonic()-wall,mechanical_cpu_seconds=time.process_time()-cpu)
 a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:v for k,v in out.items() if k not in ('detail_calls','source_duplication','same_universe_early_hints')}))
if __name__=='__main__':main()
