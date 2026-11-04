"""Select the installed Hermes interpreter before owning a coordinator lock."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


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
