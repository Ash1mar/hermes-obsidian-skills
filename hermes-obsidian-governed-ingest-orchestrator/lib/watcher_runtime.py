"""Select the installed Hermes interpreter before owning a coordinator lock."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time,json


def coordinator_pid(vault,workflow_id):
    """Observe the actual coordinator command, never a stale marker/PID alone."""
    from native_stop_inventory import workflow_processes
    rows=[r['pid'] for r in workflow_processes(workflow_id,None) if r['kind']=='reconciler']
    if len(rows)>1:raise RuntimeError('multiple native coordinators; startup remains pending')
    return rows[0] if rows else None


def confirm_handoff(vault,workflow_id,pid,*,timeout=10):
    from hermes_source_units.runtime_telemetry import runtime_directory
    if not pid:raise RuntimeError('native watcher handoff is missing; startup remains pending')
    marker=runtime_directory(vault,workflow_id)/'watcher.json';deadline=time.monotonic()+timeout
    while True:
        if coordinator_pid(vault,workflow_id)==pid:
            try:row=json.loads(marker.read_text())
            except (OSError,ValueError):row={}
            if row.get('pid')==pid and row.get('workflow_id')==workflow_id:
                return {'state':'native_coordinator_observed','pid':pid,'workflow_id':workflow_id}
        if time.monotonic()>=deadline:raise RuntimeError('native watcher did not take ownership; startup remains pending')
        time.sleep(.05)


def ensure_native_runtime():
    if importlib.util.find_spec('hermes_cli') is not None:
        return
    executable = shutil.which('hermes')
    if not executable:
        raise RuntimeError('Hermes CLI is required for native completion coordination')
    # Supports the installed venv console script, including uv shell launchers.
    python = Path(executable).resolve().parent / 'python'
    if not python.is_file():
        raise RuntimeError('Hermes CLI has no adjacent runtime interpreter')
    subprocess.run([str(python), '-c', 'import hermes_cli'], check=True, timeout=10,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    os.execv(str(python), [str(python), *sys.argv])
