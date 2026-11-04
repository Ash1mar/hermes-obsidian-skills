"""One external report after native settlement; failure never reopens domain work."""
import json
import os
from pathlib import Path
from hermes_source_units.runtime_telemetry import runtime_directory, event


def export(adapter, workflow_id):
    root=runtime_directory(adapter.workflow.vault,workflow_id)
    marker=root/'final-metrics-index.json'
    if marker.exists():return
    try:
        from workflow_metrics import collect
        db=Path(os.environ.get('HERMES_HOME',str(Path.home()/'.hermes')))/'state.db'
        result=collect(adapter.workflow,workflow_id,database=db if db.is_file() else None)
        settlement=result['efficiency']['native_settlement']
        if (settlement.get('state')!='settled' or not result['snapshot_consistent']
                or settlement.get('workflow_id')!=workflow_id
                or Path(settlement.get('vault','')).resolve()!=Path(adapter.workflow.vault).resolve()
                or settlement.get('revision')!=result['current_snapshot']['revision']):
            raise ValueError('current native settlement not proven; no final metric publication')
        dest=root/('final-metrics-'+str(result['current_snapshot']['revision'])+'.json')
        with dest.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,separators=(',',':'))
        with marker.open('x',encoding='utf-8') as stream:
            json.dump({'report':str(dest),'revision':result['current_snapshot']['revision']},stream)
        event('final_metrics_export',vault=adapter.workflow.vault,workflow_id=workflow_id,
              outcome='exported',report=str(dest),output_bytes=dest.stat().st_size)
    except Exception as exc:
        event('final_metrics_export',vault=adapter.workflow.vault,workflow_id=workflow_id,
              outcome='failed',error_class=type(exc).__name__)
