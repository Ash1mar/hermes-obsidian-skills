#!/usr/bin/env python3
"""Pin worker contracts and project a Vault ingest workflow to Hermes Kanban."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from hermes_source_units import ContractError
from orchestration import dispatch


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "sync", "cancel", "resume", "continue-workflow", "worker-begin", "worker-check",
                 "worker-register-source", "worker-prepare-source", "worker-plan-exact", "worker-heartbeat", "worker-complete", "worker-fail", "arm-canary",
                 "disarm-canary"):
        command = sub.add_parser(name)
        command.add_argument("--request", required=True)
        if name in ("worker-begin", "worker-heartbeat"):
            command.add_argument("--request-output")
    args = parser.parse_args()
    temporary = None
    try:
        output = None
        if getattr(args, "request_output", None):
            workspace = os.environ.get("HERMES_KANBAN_WORKSPACE")
            if not workspace:
                raise ValueError("request output requires a Kanban worker workspace")
            root = Path(workspace).resolve()
            output = Path(args.request_output)
            if (not output.is_absolute() or not output.parent.is_dir()
                    or not output.parent.resolve().is_relative_to(root)
                    or output.is_symlink() or (output.exists() and not output.is_file())):
                raise ValueError("request output must be a regular file within the worker workspace")
            fd, name = tempfile.mkstemp(prefix=".worker-request-", dir=output.parent)
            temporary = Path(name)
            os.close(fd)
        request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
        if output is not None and not str(request.get("node", "")).startswith("pass-slice:"):
            raise ValueError("request output is only supported for Pass slice workers")
        result = dispatch(args.vault, args.command, request)
        if output is not None:
            bound = result.get("worker_request")
            if not isinstance(bound, dict):
                raise ValueError("Pass worker did not return a bound request")
            temporary.write_text(json.dumps(bound, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, output)
            temporary = None
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        detail = {"ok": False, "error": str(exc)}
        if isinstance(exc, ContractError):
            detail.update({"code": exc.code, "path": exc.path})
        print(json.dumps(detail, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
