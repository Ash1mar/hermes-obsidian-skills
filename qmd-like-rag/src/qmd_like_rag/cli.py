from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .runtime import doctor, read_status, recall, sync


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault-root", required=True, type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--vault-id")
    parser.add_argument("--config", type=Path)


def configured(args: argparse.Namespace):
    return load_config(
        args.vault_root,
        config_path=args.config,
        state_root=args.state_root,
        vault_id=args.vault_id,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="qmd-like-rag Hermes coarse-recall provider")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    status_parser = subparsers.add_parser("status")
    add_runtime_args(status_parser)
    sync_parser = subparsers.add_parser("sync")
    add_runtime_args(sync_parser)
    sync_parser.add_argument("--rebuild", action="store_true")
    recall_parser = subparsers.add_parser("recall")
    add_runtime_args(recall_parser)
    recall_parser.add_argument("--query", required=True)
    recall_parser.add_argument("--top-k", type=int)
    serve_parser = subparsers.add_parser("serve")
    add_runtime_args(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8781)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "status":
            result = read_status(configured(args))
        elif args.command == "sync":
            result = sync(configured(args), rebuild=args.rebuild)
        elif args.command == "recall":
            result = recall(configured(args), args.query, args.top_k)
        elif args.command == "serve":
            from .service import serve

            serve(configured(args), args.host, args.port)
            return 0
        else:
            raise ValueError(f"Unsupported command: {args.command}")
        emit(result)
        return 0 if result.get("status") not in {"error", "unavailable"} else 2
    except Exception as exc:
        emit({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
