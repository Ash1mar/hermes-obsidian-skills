#!/usr/bin/env python
"""Convert an engineering PDF with MinerU into a layered document bundle v2.

The agent-facing contract stays small:

    document_bundle/
      document.md
      manifest.json
      outline.json
      images/
      tables/

Detailed MinerU outputs are preserved under ``_evidence/`` for targeted QA,
but downstream ingestion must not scan that directory by default.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUXILIARY_TYPES = {"header", "footer", "page_number", "aside_text", "page_footnote"}
IMAGE_TYPES = {"image", "chart"}
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
EVIDENCE_PATTERNS = (
    "*content_list.json",
    "*content_list_v2.json",
    "*middle.json",
    "*model.json",
    "*layout.pdf",
    "*span.pdf",
)


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected boolean value, got {value!r}")


def run_mineru(args: argparse.Namespace, work_dir: Path) -> None:
    mineru_command = args.mineru_command or os.environ.get("MINERU_COMMAND") or "mineru"
    cmd = [
        mineru_command,
        "-p",
        str(args.input),
        "-o",
        str(work_dir),
        "-b",
        args.backend,
        "-m",
        args.method,
        "--formula",
        str(args.formula).lower(),
        "--table",
        str(args.table).lower(),
        "--image-analysis",
        str(args.image_analysis).lower(),
    ]

    if args.effort:
        cmd.extend(["--effort", args.effort])
    if args.lang:
        cmd.extend(["-l", args.lang])
    if args.api_url:
        cmd.extend(["--api-url", args.api_url])
    if args.start is not None:
        cmd.extend(["-s", str(args.start)])
    if args.end is not None:
        cmd.extend(["-e", str(args.end)])

    env = os.environ.copy()
    if args.model_source != "auto":
        env["MINERU_MODEL_SOURCE"] = args.model_source

    try:
        subprocess.run(cmd, check=True, env=env)
    except FileNotFoundError:
        print(
            "MinerU CLI not found. Install MinerU in the project/WSL environment, "
            "set MINERU_COMMAND, pass --mineru-command, or pass --from-mineru-output.",
            file=sys.stderr,
        )
        raise SystemExit(127)
    except subprocess.CalledProcessError as exc:
        print(f"MinerU conversion failed with exit code {exc.returncode}.", file=sys.stderr)
        raise SystemExit(exc.returncode)


def score_output_path(path: Path, source_stem: str, suffix_hint: str) -> tuple[int, int]:
    text = path.as_posix().lower()
    score = 0
    if source_stem.lower() in text:
        score += 10
    if path.name.lower().endswith(suffix_hint):
        score += 5
    return score, -len(path.parts)


def choose_output_file(root: Path, patterns: list[str], source_stem: str, suffix_hint: str) -> Path | None:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(root.rglob(pattern))
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: score_output_path(p, source_stem, suffix_hint), reverse=True)[0]


def load_content_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        # Prefer the legacy flat content list. MinerU content_list_v2 is page-grouped
        # and is retained in _evidence rather than used as the ingest view.
        if data and all(isinstance(item, list) for item in data):
            raise ValueError(f"Page-grouped content_list_v2 is not a flat content list: {path}")
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("content_list", "data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise ValueError(f"Unsupported content list shape: {path}")


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip()).strip()
    return str(value).strip()


def item_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = text_value(item.get(key))
        if value:
            return value
    return ""


def item_page(item: dict[str, Any]) -> int:
    try:
        return int(item.get("page_idx", 0)) + 1
    except (TypeError, ValueError):
        return 1


def item_caption(item: dict[str, Any], sequence: int) -> str:
    caption = item_text(item, "image_caption", "chart_caption", "caption", "title", "text")
    if caption:
        return caption
    page = item_page(item)
    label = "Chart" if item.get("type") == "chart" else "Figure"
    return f"{label} p{page}-{sequence}"


def normalize_id(raw: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", raw).strip("_").lower()
    return normalized or "item"


def evidence_id(caption: str, page: int, sequence: int, kind: str, used: set[str]) -> str:
    patterns = [
        (r"(?:图|figure|fig\.?)\s*([0-9A-Za-z]+(?:[.\-][0-9A-Za-z]+)*)", "fig"),
        (r"(?:表|table)\s*([0-9A-Za-z]+(?:[.\-][0-9A-Za-z]+)*)", "table"),
    ]
    base = ""
    for pattern, prefix in patterns:
        match = re.search(pattern, caption, flags=re.IGNORECASE)
        if match and (kind == prefix or kind == "fig"):
            base = f"{prefix}_{normalize_id(match.group(1))}"
            break
    if not base:
        base = f"{kind}_p{page}_n{sequence}"

    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}_{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def resolve_asset(asset_path: str, search_roots: list[Path]) -> Path | None:
    if not asset_path:
        return None
    path = Path(asset_path)
    if path.is_absolute() and path.exists():
        return path
    for root in search_roots:
        candidate = root / path
        if candidate.exists():
            return candidate
    if path.name:
        for root in search_roots:
            matches = list(root.rglob(path.name))
            if matches:
                return matches[0]
    return None


def copy_unique_asset(source: Path | None, target_dir: Path, asset_id: str, preferred_suffix: str) -> tuple[str, bool]:
    suffix = preferred_suffix.lower()
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        suffix = ".png"
    target = target_dir / f"{asset_id}{suffix}"
    if source and source.exists() and source.stat().st_size > 0:
        shutil.copy2(source, target)
        return target.as_posix(), True
    return target.as_posix(), False


def structural_heading_level(text: str, raw_level: int, current_level: int, first_heading: bool) -> int:
    stripped = text.strip()
    if first_heading:
        return 1
    if re.match(r"^目\s*录$", stripped):
        return 2
    if re.match(r"^附录\s*[0-9一二三四五六七八九十]+", stripped):
        return 2
    if re.match(r"^[（(]\s*\d+\s*[）)]", stripped) or re.match(r"^\d+\s*[）)]", stripped):
        return 0

    numbered = re.match(r"^(\d+(?:\.\d+)*)(\.?)\s*", stripped)
    if numbered:
        number = numbered.group(1)
        trailing_dot = numbered.group(2)
        depth = len(number.split("."))
        if depth == 1 and trailing_dot:
            return 0
        return min(depth + 1, 6)

    if raw_level > 0:
        return min(max(current_level + 1, 2), 6)
    return 0


def update_heading_stack(stack: list[dict[str, Any]], text: str, level: int) -> None:
    while stack and int(stack[-1]["level"]) >= level:
        stack.pop()
    stack.append({"title": text, "level": level})


def current_section_titles(stack: list[dict[str, Any]]) -> list[str]:
    return [str(item["title"]) for item in stack if item.get("title")]


def prettify_html_table(body: str) -> str:
    if "<table" not in body.lower():
        return body
    return re.sub(r">\s*<", ">\n<", body.strip())


def render_document(
    items: list[dict[str, Any]],
    bundle_dir: Path,
    mineru_root: Path,
    content_list_path: Path,
    markdown_path: Path | None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    image_dir = bundle_dir / "images"
    table_dir = bundle_dir / "tables"
    image_dir.mkdir(parents=True, exist_ok=True)
    table_dir.mkdir(parents=True, exist_ok=True)

    search_roots = [content_list_path.parent, mineru_root]
    if markdown_path:
        search_roots.insert(0, markdown_path.parent)

    lines: list[str] = []
    images: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    headings: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    image_sequence = 0
    table_sequence = 0
    equation_sequence = 0
    last_page: int | None = None
    first_heading = True
    raw_heading_levels: list[int] = []

    counts = {"text": 0, "image": 0, "chart": 0, "table": 0, "equation": 0, "list": 0, "code": 0}

    def emit(values: list[str]) -> int:
        start_line = len(lines) + 1
        lines.extend(values)
        return start_line

    def page_anchor(page: int) -> None:
        nonlocal last_page
        if page == last_page:
            return
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend([f"<!-- source-page: {page} -->", ""])
        last_page = page

    for sequence, item in enumerate(items, start=1):
        item_type = str(item.get("type", "")).strip()
        if item_type in AUXILIARY_TYPES:
            continue

        page = item_page(item)
        page_anchor(page)
        block_id = f"block_{sequence:05d}"
        line_start = len(lines) + 1

        if item_type == "text":
            counts["text"] += 1
            text = item_text(item, "text", "content")
            if not text:
                continue
            level_raw = item.get("text_level", 0) or 0
            try:
                raw_level = int(level_raw)
            except (TypeError, ValueError):
                raw_level = 0
            if raw_level > 0:
                raw_heading_levels.append(raw_level)
                current_level = int(headings[-1]["level"]) if headings else 0
                level = structural_heading_level(text, raw_level, current_level, first_heading)
                if level > 0:
                    update_heading_stack(headings, text, level)
                    line_start = emit([f"{'#' * level} {text}", ""])
                    first_heading = False
                else:
                    line_start = emit([f"**{text}**", ""])
            else:
                line_start = emit([text, ""])

        elif item_type in IMAGE_TYPES:
            counts[item_type] += 1
            image_sequence += 1
            caption = item_caption(item, image_sequence)
            image_id = evidence_id(caption, page, image_sequence, "fig", used_ids)
            img_path = item_text(item, "img_path", "image_path", "path")
            source_image = resolve_asset(img_path, search_roots)
            suffix = source_image.suffix if source_image else Path(img_path).suffix
            target_path, copied = copy_unique_asset(source_image, image_dir, image_id, suffix)
            relative_path = Path(target_path).relative_to(bundle_dir).as_posix()
            line_start = emit([f"![{caption}]({relative_path})", ""])
            if not copied:
                issues.append({
                    "code": "missing-image-asset",
                    "severity": "fail",
                    "page": page,
                    "message": f"Image asset could not be resolved: {img_path or image_id}",
                })
            images.append({
                "id": image_id,
                "caption": caption,
                "page": page,
                "section_path": current_section_titles(headings),
                "path": relative_path,
                "type": item_type,
                "bbox": item.get("bbox"),
                "source_img_path": img_path,
                "line": line_start,
                "quality": "pass" if copied else "fail",
            })

        elif item_type == "table":
            counts["table"] += 1
            table_sequence += 1
            caption = item_text(item, "table_caption", "caption")
            body = item_text(item, "table_body", "html", "text", "content")
            img_path = item_text(item, "img_path", "image_path", "path")

            if not body and tables and not caption and page <= int(tables[-1]["page_end"]) + 1:
                tables[-1]["page_end"] = max(int(tables[-1]["page_end"]), page)
                tables[-1]["quality"] = "warn"
                issues.append({
                    "code": "cross-page-table-continuation-unresolved",
                    "severity": "warn",
                    "page": page,
                    "table_id": tables[-1]["id"],
                    "message": "A body-less table block was associated with the previous table as a possible continuation.",
                })
                line_start = emit([f"<!-- table-continuation: {tables[-1]['id']}; source-page: {page}; qa: warn -->", ""])
            else:
                display_caption = caption or f"Table p{page}-{table_sequence}"
                table_id = evidence_id(display_caption, page, table_sequence, "table", used_ids)
                source_image = resolve_asset(img_path, search_roots)
                evidence_path: str | None = None
                if source_image:
                    copied_path, copied = copy_unique_asset(
                        source_image,
                        table_dir,
                        f"{table_id}_source",
                        source_image.suffix,
                    )
                    if copied:
                        evidence_path = Path(copied_path).relative_to(bundle_dir).as_posix()

                table_md_path = table_dir / f"{table_id}.md"
                table_md_lines = [
                    f"# {display_caption}",
                    "",
                    f"<!-- table-id: {table_id}; source-page: {page} -->",
                    "",
                ]
                if body:
                    table_md_lines.extend([prettify_html_table(body), ""])
                else:
                    table_md_lines.extend(["<!-- table-body-unavailable -->", ""])
                    issues.append({
                        "code": "table-body-unavailable",
                        "severity": "warn",
                        "page": page,
                        "table_id": table_id,
                        "message": "MinerU did not provide a table body; use the page evidence for review.",
                    })
                if evidence_path:
                    table_md_lines.extend([
                        "## 页面证据",
                        "",
                        f"![{display_caption}]({Path(evidence_path).name})",
                        "",
                    ])
                table_md_path.write_text("\n".join(table_md_lines), encoding="utf-8")

                relative_table_path = table_md_path.relative_to(bundle_dir).as_posix()
                line_start = emit([
                    f"**{display_caption}**",
                    "",
                    f"[查看表格]({relative_table_path})",
                    "",
                    f"<!-- table-id: {table_id}; source-page: {page}; qa: {'pass' if body else 'warn'} -->",
                    "",
                ])
                tables.append({
                    "id": table_id,
                    "caption": display_caption,
                    "page_start": page,
                    "page_end": page,
                    "section_path": current_section_titles(headings),
                    "path": relative_table_path,
                    "evidence_path": evidence_path,
                    "bbox": item.get("bbox"),
                    "source_img_path": img_path,
                    "line": line_start,
                    "quality": "pass" if body else "warn",
                })

        elif item_type == "equation":
            counts["equation"] += 1
            equation_sequence += 1
            equation = item_text(item, "text", "latex", "content")
            if equation:
                equation_id = f"eq_p{page}_n{equation_sequence}"
                line_start = emit([
                    f"<!-- block-id: {equation_id}; source-page: {page}; qa: review -->",
                    "",
                    equation,
                    "",
                ])

        elif item_type == "list":
            counts["list"] += 1
            list_items = item.get("list_items")
            values: list[str] = []
            if isinstance(list_items, list):
                for value in list_items:
                    text = text_value(value)
                    if text:
                        values.append(f"- {text}")
            else:
                text = item_text(item, "text", "content")
                if text:
                    values.append(text)
            if values:
                values.append("")
                line_start = emit(values)

        elif item_type == "code":
            counts["code"] += 1
            code = item_text(item, "code_body", "text", "content")
            if code:
                line_start = emit(["```", code, "```", ""])

        else:
            fallback = item_text(item, "text", "content")
            if fallback:
                line_start = emit([fallback, ""])

        blocks.append({
            "block_id": block_id,
            "type": item_type,
            "page": page,
            "bbox": item.get("bbox"),
            "line": line_start,
            "section_path": current_section_titles(headings),
            "payload": item,
        })

    text = "\n".join(lines).strip() + "\n"
    if not text and markdown_path:
        text = markdown_path.read_text(encoding="utf-8")

    if raw_heading_levels and len(set(raw_heading_levels)) == 1 and len(raw_heading_levels) > 10:
        issues.append({
            "code": "heading-levels-reconstructed",
            "severity": "info",
            "message": "MinerU emitted one repeated heading level; numbered headings were reconstructed deterministically.",
        })

    return text, images, tables, blocks, issues, counts


def section_base_id(title: str, index: int) -> str:
    appendix = re.match(r"^附录\s*([0-9一二三四五六七八九十]+)", title.strip())
    if appendix:
        return f"appendix-{normalize_id(appendix.group(1))}"
    numbered = re.match(r"^(\d+(?:\.\d+)*)", title.strip())
    if numbered:
        return numbered.group(1)
    if re.match(r"^目\s*录$", title.strip()):
        return "toc"
    return f"section-{index:04d}"


def build_outline(document_text: str, assets: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    lines = document_text.splitlines()
    page_by_line: list[int | None] = []
    current_page: int | None = None
    entries: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for line_number, line in enumerate(lines, start=1):
        page_match = re.match(r"<!-- source-page:\s*(\d+)\s*-->", line.strip())
        if page_match:
            current_page = int(page_match.group(1))
        page_by_line.append(current_page)

        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not heading:
            continue
        level = len(heading.group(1))
        title = heading.group(2).strip()
        base = section_base_id(title, len(entries) + 1)
        section_id = base
        suffix = 2
        while section_id in used_ids:
            section_id = f"{base}-{suffix}"
            suffix += 1
        used_ids.add(section_id)

        while stack and int(stack[-1]["level"]) >= level:
            stack.pop()
        parent = stack[-1]["id"] if stack else None
        path = [str(item["id"]) for item in stack] + [section_id]
        entry = {
            "id": section_id,
            "title": title,
            "level": level,
            "parent": parent,
            "path": path,
            "start_line": line_number,
            "end_line": len(lines),
            "pages": [current_page] if current_page is not None else [],
            "assets": [],
            "quality": "pass",
        }
        entries.append(entry)
        stack.append(entry)

    for index, entry in enumerate(entries):
        end_line = len(lines)
        for following in entries[index + 1 :]:
            if int(following["level"]) <= int(entry["level"]):
                end_line = int(following["start_line"]) - 1
                break
        entry["end_line"] = end_line
        pages = {
            page
            for page in page_by_line[int(entry["start_line"]) - 1 : end_line]
            if page is not None
        }
        entry["pages"] = sorted(pages)

    def deepest_section(line_number: int) -> dict[str, Any] | None:
        matches = [
            entry
            for entry in entries
            if int(entry["start_line"]) <= line_number <= int(entry["end_line"])
        ]
        return max(matches, key=lambda item: int(item["level"])) if matches else None

    for asset in assets:
        section = deepest_section(int(asset.get("line", 1)))
        if section:
            asset["section_id"] = section["id"]
            section["assets"].append(asset["id"])
            if asset.get("quality") in {"warn", "fail"}:
                section["quality"] = asset["quality"]

    for block in blocks:
        section = deepest_section(int(block.get("line", 1)))
        if section:
            block["section_id"] = section["id"]

    return {
        "schema_version": "2.0",
        "document": "document.md",
        "sections": entries,
    }


def prepare_bundle_dir(bundle_dir: Path, overwrite: bool) -> None:
    if bundle_dir.exists():
        if not overwrite and any(bundle_dir.iterdir()):
            print(f"Output bundle is not empty, pass --overwrite to replace: {bundle_dir}", file=sys.stderr)
            raise SystemExit(2)
        for child in bundle_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    bundle_dir.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mineru_version() -> str | None:
    try:
        return importlib.metadata.version("mineru")
    except importlib.metadata.PackageNotFoundError:
        return None


def copy_selected_evidence(mineru_root: Path, bundle_dir: Path) -> list[str]:
    evidence_root = bundle_dir / "_evidence" / "mineru"
    copied: list[str] = []
    seen: set[Path] = set()
    for pattern in EVIDENCE_PATTERNS:
        for source in mineru_root.rglob(pattern):
            if not source.is_file() or source in seen:
                continue
            seen.add(source)
            relative = source.relative_to(mineru_root)
            target = evidence_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target.relative_to(bundle_dir).as_posix())
    return sorted(copied)


def write_blocks_jsonl(bundle_dir: Path, blocks: list[dict[str, Any]]) -> str:
    evidence_dir = bundle_dir / "_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / "blocks.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for block in blocks:
            handle.write(json.dumps(block, ensure_ascii=False) + "\n")
    return path.relative_to(bundle_dir).as_posix()


def quality_status(issues: list[dict[str, Any]]) -> str:
    severities = {str(issue.get("severity")) for issue in issues}
    if "fail" in severities:
        return "fail"
    if "warn" in severities:
        return "warn"
    return "pass"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert a PDF with MinerU and create a layered document bundle v2."
    )
    parser.add_argument("input", type=Path, help="Input PDF path")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output document_bundle directory")
    parser.add_argument(
        "--from-mineru-output",
        type=Path,
        help="Reuse an existing MinerU output directory instead of running the MinerU CLI",
    )
    parser.add_argument(
        "--record-conversion-settings",
        action="store_true",
        help=(
            "With --from-mineru-output, record the supplied backend/method/feature settings "
            "instead of marking them unknown. Use only when the original settings are known."
        ),
    )
    parser.add_argument(
        "--mineru-command",
        help="MinerU executable path. Defaults to MINERU_COMMAND or mineru on PATH.",
    )
    parser.add_argument("--backend", default="hybrid-engine", help="MinerU backend")
    parser.add_argument("--method", default="auto", choices=["auto", "txt", "ocr"], help="MinerU parsing method")
    parser.add_argument("--effort", choices=["medium", "high"], default="high", help="Hybrid parsing effort")
    parser.add_argument("--lang", help="MinerU OCR language hint, such as ch")
    parser.add_argument("--api-url", help="Existing mineru-api base URL")
    parser.add_argument("--start", type=int, help="Start page, 0-based")
    parser.add_argument("--end", type=int, help="End page, 0-based")
    parser.add_argument("--formula", type=parse_bool, default=True, help="Enable formula parsing")
    parser.add_argument("--table", type=parse_bool, default=True, help="Enable table parsing")
    parser.add_argument(
        "--image-analysis",
        type=parse_bool,
        default=False,
        help="Enable image/chart semantic analysis. Default false keeps images as visual evidence.",
    )
    parser.add_argument(
        "--model-source",
        choices=["auto", "local", "huggingface", "modelscope"],
        default="auto",
        help="MinerU model source. Use local for the repaired offline environment.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing bundle directory")
    parser.add_argument(
        "--keep-mineru-output",
        action="store_true",
        help="Keep the complete raw MinerU output under _evidence/mineru_raw",
    )
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="Do not preserve selected MinerU QA files or blocks.jsonl",
    )
    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    args.input = input_path
    bundle_dir = args.output.expanduser().resolve()
    if not input_path.exists():
        print(f"Input does not exist: {input_path}", file=sys.stderr)
        return 2
    if input_path.suffix.lower() != ".pdf":
        print(f"Expected a PDF input, got: {input_path}", file=sys.stderr)
        return 2

    prepare_bundle_dir(bundle_dir, args.overwrite)

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.from_mineru_output:
        mineru_root = args.from_mineru_output.expanduser().resolve()
        if not mineru_root.exists():
            print(f"MinerU output directory does not exist: {mineru_root}", file=sys.stderr)
            return 2
    else:
        if args.keep_mineru_output:
            mineru_root = bundle_dir / "_evidence" / "mineru_raw"
            mineru_root.mkdir(parents=True, exist_ok=True)
        else:
            temp_dir = tempfile.TemporaryDirectory(prefix="mineru-bundle-")
            mineru_root = Path(temp_dir.name)
        run_mineru(args, mineru_root)

    source_stem = input_path.stem
    content_list_path = choose_output_file(
        mineru_root,
        ["*content_list.json"],
        source_stem,
        "content_list.json",
    )
    markdown_path = choose_output_file(mineru_root, ["*.md"], source_stem, ".md")
    issues: list[dict[str, Any]] = []

    if not content_list_path:
        if not markdown_path:
            print(f"No MinerU Markdown or flat content list found under: {mineru_root}", file=sys.stderr)
            return 1
        document_text = markdown_path.read_text(encoding="utf-8")
        images: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        counts = {"text": 0, "image": 0, "chart": 0, "table": 0, "equation": 0, "list": 0, "code": 0}
        issues.append({
            "code": "content-list-missing",
            "severity": "warn",
            "message": "Only MinerU Markdown was found; page/block evidence and asset indexing are incomplete.",
        })
    else:
        items = load_content_list(content_list_path)
        document_text, images, tables, blocks, render_issues, counts = render_document(
            items,
            bundle_dir,
            mineru_root,
            content_list_path,
            markdown_path,
        )
        issues.extend(render_issues)

    document_path = bundle_dir / "document.md"
    document_path.write_text(document_text, encoding="utf-8", newline="\n")

    outline = build_outline(document_text, images + tables, blocks)
    if not outline["sections"]:
        issues.append({
            "code": "outline-empty",
            "severity": "warn",
            "message": "No usable heading outline was produced; downstream ingestion must use manual ranges.",
        })
    outline_path = bundle_dir / "outline.json"
    outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    evidence_files: list[str] = []
    blocks_path: str | None = None
    if not args.no_evidence:
        if args.keep_mineru_output and not args.from_mineru_output:
            for pattern in EVIDENCE_PATTERNS:
                for path in mineru_root.rglob(pattern):
                    if path.is_file():
                        evidence_files.append(path.relative_to(bundle_dir).as_posix())
            evidence_files = sorted(set(evidence_files))
        else:
            evidence_files = copy_selected_evidence(mineru_root, bundle_dir)
        blocks_path = write_blocks_jsonl(bundle_dir, blocks)
    else:
        issues.append({
            "code": "evidence-not-preserved",
            "severity": "info",
            "message": "Selected MinerU QA artifacts were not preserved by request.",
        })

    if source_stem.lower() not in bundle_dir.name.lower():
        issues.append({
            "code": "bundle-name-source-mismatch",
            "severity": "warn",
            "message": "Bundle directory name does not contain the source PDF stem; verify document identity.",
        })

    parsed_pages = max((int(block.get("page", 1)) for block in blocks), default=0)
    review_required: list[str] = []
    if counts.get("equation", 0):
        review_required.append("formula-semantics")
    if counts.get("table", 0):
        review_required.append("table-and-cross-page-structure")
    if counts.get("image", 0) or counts.get("chart", 0):
        review_required.append("figure-internals-when-used-as-evidence")

    settings_known = not args.from_mineru_output or args.record_conversion_settings
    manifest = {
        "schema_version": "2.0",
        "profile": "engineering",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(input_path),
            "filename": input_path.name,
            "sha256": sha256_file(input_path),
            "parsed_pages": parsed_pages,
        },
        "conversion": {
            "engine": "MinerU",
            "engine_version": mineru_version(),
            "backend": args.backend if settings_known else None,
            "method": args.method if settings_known else None,
            "effort": args.effort if settings_known else None,
            "formula": args.formula if settings_known else None,
            "table": args.table if settings_known else None,
            "image_analysis": args.image_analysis if settings_known else None,
            "model_source": args.model_source if settings_known else None,
            "mineru_output": str(mineru_root) if args.keep_mineru_output or args.from_mineru_output else None,
        },
        "document": {
            "path": "document.md",
            "line_count": len(document_text.splitlines()),
        },
        "outline": {
            "path": "outline.json",
            "section_count": len(outline["sections"]),
        },
        "images": images,
        "tables": tables,
        "evidence": {
            "default_ingest": False,
            "files": evidence_files,
            "blocks": blocks_path,
        },
        "features": {
            "page_anchors": "<!-- source-page:" in document_text,
            "outline": bool(outline["sections"]),
            "external_tables": bool(tables),
            "figure_assets": bool(images),
            "evidence_archive": bool(evidence_files or blocks_path),
        },
        "counts": counts,
        "quality": {
            "status": quality_status(issues),
            "issues": issues,
            "review_required": review_required,
        },
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    if temp_dir:
        temp_dir.cleanup()

    print(f"Bundle: {bundle_dir}")
    print(f"Schema: {manifest['schema_version']} ({manifest['profile']})")
    print(f"Quality: {manifest['quality']['status']}")
    print(f"Sections: {len(outline['sections'])}")
    print(f"Images: {len(images)}")
    print(f"Tables: {len(tables)}")
    return 0 if manifest["quality"]["status"] != "fail" else 3


if __name__ == "__main__":
    raise SystemExit(main())
