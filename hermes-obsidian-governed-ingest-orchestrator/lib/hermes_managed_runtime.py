"""Select the Python committed by the Hermes installation manager."""

import os
from pathlib import Path
import sys


DEFAULT_HERMES_ROOT = Path("/usr/local/lib/hermes-agent")


class ManagedPythonUnavailable(RuntimeError):
    """Hermes has no executable Python in its committed environment."""


def committed_python(root=DEFAULT_HERMES_ROOT):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    try:
        from pm.environments import committed_venv

        environment = committed_venv(root)
    finally:
        sys.path.remove(str(root))
    python = environment / "bin/python3" if environment else None
    if not python or not python.is_file() or not os.access(python, os.X_OK):
        raise ManagedPythonUnavailable(f"No executable Python in the committed Hermes environment: {root}")
    return python


def clean_python_environment(environment):
    return {key: value for key, value in environment.items() if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}}
