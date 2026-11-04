"""Select the installed Hermes interpreter before owning a coordinator lock."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time,json
import shlex


def native_interpreter(executable):
    """Resolve console scripts or literal shell exec wrappers without shell eval."""
    cli=Path(executable).resolve();seen=set()
    for _ in range(4):
        if cli in seen:break
        seen.add(cli)
        python=cli.parent/'python'
        if python.is_file():return python
        try:lines=cli.read_text(encoding='utf-8').splitlines()
        except (OSError,UnicodeError):break
        targets=[]
        for line in lines:
            try:words=shlex.split(line)
            except ValueError:continue
            if (len(words)==3 and words[0]=='exec' and words[2]=='$@'
                    and Path(words[1]).is_absolute() and Path(words[1]).name=='hermes'
                    and Path(words[1]).is_file()):targets.append(Path(words[1]).resolve())
        if len(targets)!=1:break
        cli=targets[0]
    raise RuntimeError('Hermes CLI has no verifiable native runtime interpreter')


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
    python = native_interpreter(executable)
    subprocess.run([str(python), '-c', 'import hermes_cli'], check=True, timeout=10,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    os.execv(str(python), [str(python), *sys.argv])
