#!/usr/bin/env python3
"""Start the console with the environment selected by the Hermes installation."""

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-obsidian-governed-ingest-orchestrator/lib"))
from hermes_managed_runtime import (DEFAULT_HERMES_ROOT, ManagedPythonUnavailable,
                                    clean_python_environment, committed_python)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, default=DEFAULT_HERMES_ROOT)
    parser.add_argument("console_args", nargs=argparse.REMAINDER, help="console options after --")
    args = parser.parse_args()
    root = args.hermes_root.resolve()
    try:
        python = committed_python(root)
    except ManagedPythonUnavailable:
        parser.exit(2, f"Hermes 安装没有可用的托管 Python：{root}\n")
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        parser.exit(2, f"无法读取 Hermes 托管环境（{root}）：{error}\n")

    entry = Path(__file__).with_name("server.py").resolve()
    console_args = args.console_args
    if console_args[:1] == ["--"]:
        console_args = console_args[1:]
    env = clean_python_environment(os.environ)
    os.execve(str(python), [str(python), "-B", str(entry), *console_args], env)


if __name__ == "__main__":
    main()
