#!/usr/bin/env python3
"""Start the console with the environment selected by the Hermes installation."""

import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, default=Path("/usr/local/lib/hermes-agent"))
    parser.add_argument("console_args", nargs=argparse.REMAINDER, help="console options after --")
    args = parser.parse_args()
    root = args.hermes_root.resolve()
    sys.path.insert(0, str(root))
    try:
        from pm.environments import committed_venv

        environment = committed_venv(root)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        parser.exit(2, f"无法读取 Hermes 托管环境（{root}）：{error}\n")
    python = environment / "bin/python3" if environment else None
    if not python or not python.is_file() or not os.access(python, os.X_OK):
        parser.exit(2, f"Hermes 安装没有可用的托管 Python：{root}\n")

    entry = Path(__file__).with_name("server.py").resolve()
    console_args = args.console_args
    if console_args[:1] == ["--"]:
        console_args = console_args[1:]
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}}
    os.execve(str(python), [str(python), "-B", str(entry), *console_args], env)


if __name__ == "__main__":
    main()
