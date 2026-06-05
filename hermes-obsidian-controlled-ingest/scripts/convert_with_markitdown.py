#!/usr/bin/env python
"""
Convert a source file to Markdown for governed Obsidian ingestion.

This script preserves the original file and writes converted Markdown to the
requested output path. It requires MarkItDown to be installed in the active
Python environment or available as a CLI command.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def convert_with_python_api(input_path: Path) -> str:
    from markitdown import MarkItDown  # type: ignore

    converter = MarkItDown(enable_plugins=False)
    result = converter.convert(str(input_path))
    return result.text_content


def convert_with_cli(input_path: Path) -> str:
    completed = subprocess.run(
        ["markitdown", str(input_path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert a non-Markdown source file to Markdown with MarkItDown."
    )
    parser.add_argument("input", help="Source file path")
    parser.add_argument("-o", "--output", required=True, help="Output Markdown path")
    parser.add_argument(
        "--method",
        choices=["auto", "python-api", "cli"],
        default="auto",
        help="Conversion method",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting an existing output file",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    if not input_path.exists():
        print(f"Input does not exist: {input_path}", file=sys.stderr)
        return 2

    if output_path.exists() and not args.overwrite:
        print(f"Output already exists, pass --overwrite to replace: {output_path}", file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    text: str | None = None

    if args.method in ("auto", "python-api"):
        try:
            text = convert_with_python_api(input_path)
        except Exception as exc:  # pragma: no cover - best-effort fallback
            errors.append(f"python-api failed: {exc}")
            if args.method == "python-api":
                print(errors[-1], file=sys.stderr)
                return 1

    if text is None and args.method in ("auto", "cli"):
        try:
            text = convert_with_cli(input_path)
        except Exception as exc:
            errors.append(f"cli failed: {exc}")
            print("\n".join(errors), file=sys.stderr)
            return 1

    if text is None:
        print("No conversion output produced.", file=sys.stderr)
        return 1

    output_path.write_text(text, encoding="utf-8")
    print(f"Converted: {input_path}")
    print(f"Output: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

