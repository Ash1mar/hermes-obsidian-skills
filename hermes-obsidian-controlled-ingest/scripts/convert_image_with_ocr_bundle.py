#!/usr/bin/env python
"""Create a governed Bundle v2 from standalone image sources.

Standalone images are different from figures embedded in a PDF: when the image
is the source itself, OCR may be the only text layer. This helper keeps the
original image as evidence, records any OCR text as derived content, and marks
the bundle for review instead of treating OCR as authoritative.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
MATERIAL_TYPES = {"scanned-page", "table-image", "figure-image", "photo", "mixed"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_stem(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value).strip("._")
    return cleaned or "image"


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


def load_ocr_text_files(paths: list[Path], image_count: int) -> list[str | None]:
    if not paths:
        return [None] * image_count
    if len(paths) == 1 and image_count == 1:
        return [paths[0].read_text(encoding="utf-8-sig").strip()]
    if len(paths) != image_count:
        raise ValueError("--ocr-text-file must be supplied once per image, unless there is only one image")
    return [path.read_text(encoding="utf-8-sig").strip() for path in paths]


def run_ocr_command(command_template: str, image: Path) -> str:
    command = command_template.format(input=str(image))
    completed = subprocess.run(
        command,
        shell=True,
        check=True,
        text=True,
        capture_output=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def normalize_inputs(values: list[Path]) -> list[Path]:
    images: list[Path] = []
    for value in values:
        path = value.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Input image does not exist: {path}")
        if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            raise ValueError(f"Unsupported image suffix for controlled image bundle: {path}")
        images.append(path)
    return images


def build_bundle(args: argparse.Namespace) -> dict[str, Any]:
    input_images = normalize_inputs(args.input)
    bundle_dir = args.output.expanduser().resolve()
    prepare_bundle_dir(bundle_dir, args.overwrite)

    image_dir = bundle_dir / "images"
    evidence_dir = bundle_dir / "_evidence"
    image_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    ocr_texts = load_ocr_text_files([path.expanduser().resolve() for path in args.ocr_text_file], len(input_images))
    records: list[dict[str, Any]] = []
    manifest_images: list[dict[str, Any]] = []
    document_lines: list[str] = []
    sections: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    source_hashes: list[str] = []
    text_count = 0

    for page, image in enumerate(input_images, start=1):
        source_hash = sha256_file(image)
        source_hashes.append(source_hash)
        suffix = image.suffix.lower()
        image_id = f"page_{page:03d}"
        target = image_dir / f"{image_id}{suffix}"
        shutil.copy2(image, target)
        relative_image = target.relative_to(bundle_dir).as_posix()

        ocr_text = ocr_texts[page - 1]
        if args.ocr_command:
            ocr_text = run_ocr_command(args.ocr_command, image)
        if ocr_text:
            text_count += 1

        section_start = len(document_lines) + 1
        document_lines.extend(
            [
                f"<!-- source-page: {page} -->",
                "",
                f"# Image page {page}",
                "",
                f"![Image page {page}]({relative_image})",
                "",
            ]
        )
        if ocr_text:
            document_lines.extend(
                [
                    "<!-- ocr-derived: true; qa: review -->",
                    "",
                    "## OCR Text",
                    "",
                    ocr_text,
                    "",
                ]
            )
        else:
            document_lines.extend(
                [
                    "<!-- image-only-source: true; qa: review -->",
                    "",
                    "OCR text is unavailable. Treat this page as visual evidence until targeted OCR or manual review is completed.",
                    "",
                ]
            )
            issues.append(
                {
                    "code": "ocr-text-unavailable",
                    "severity": "warn",
                    "page": page,
                    "message": "Standalone image page has no OCR text; use visual evidence or targeted OCR before extraction.",
                }
            )

        section_end = len(document_lines) - 1 if document_lines and document_lines[-1] == "" else len(document_lines)
        sections.append(
            {
                "id": image_id,
                "title": f"Image page {page}",
                "level": 1,
                "parent": None,
                "path": [image_id],
                "start_line": section_start + 2,
                "end_line": section_end,
                "pages": [page],
                "assets": [image_id],
                "quality": "warn",
            }
        )
        manifest_images.append(
            {
                "id": image_id,
                "caption": f"Standalone image page {page}",
                "page": page,
                "section_path": [f"Image page {page}"],
                "path": relative_image,
                "type": "source_image",
                "bbox": None,
                "source_img_path": str(image),
                "line": section_start + 4,
                "quality": "pass",
                "section_id": image_id,
            }
        )
        records.append(
            {
                "block_id": f"image_{page:05d}",
                "type": "source_image",
                "page": page,
                "line": section_start,
                "source_path": str(image),
                "source_sha256": source_hash,
                "image_path": relative_image,
                "ocr_text": ocr_text or "",
                "ocr_derived": bool(ocr_text),
                "material_type": args.material_type,
            }
        )

    document_text = "\n".join(document_lines).rstrip() + "\n"
    (bundle_dir / "document.md").write_text(document_text, encoding="utf-8", newline="\n")
    outline = {"schema_version": "2.0", "document": "document.md", "sections": sections}
    (bundle_dir / "outline.json").write_text(
        json.dumps(outline, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    blocks_path = evidence_dir / "ocr_blocks.jsonl"
    blocks_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )

    combined_hash = hashlib.sha256("".join(source_hashes).encode("ascii")).hexdigest()
    if len(input_images) == 1:
        source_path = str(input_images[0])
        source_filename = input_images[0].name
        source_sha = source_hashes[0]
    else:
        source_path = ";".join(str(path) for path in input_images)
        source_filename = f"{safe_stem(input_images[0].stem)}-image-set"
        source_sha = combined_hash

    issues.append(
        {
            "code": "standalone-image-source-review-required",
            "severity": "warn",
            "message": "Standalone image OCR and visual content require review before promotion to authoritative knowledge.",
        }
    )
    review_required = ["image-source-visual-review"]
    if text_count:
        review_required.append("ocr-text-verification")
    if args.material_type == "table-image":
        review_required.append("table-structure")
    if args.material_type == "figure-image":
        review_required.append("figure-internals-when-used-as-evidence")

    manifest = {
        "schema_version": "2.0",
        "profile": "engineering",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": {
            "path": source_path,
            "filename": source_filename,
            "sha256": source_sha,
            "parsed_pages": len(input_images),
            "source_type": "standalone-image",
            "material_type": args.material_type,
        },
        "conversion": {
            "engine": "Hermes image bundle",
            "engine_version": "1.0",
            "ocr_command": args.ocr_command,
            "ocr_text_files": [str(path) for path in args.ocr_text_file],
        },
        "document": {"path": "document.md", "line_count": len(document_text.splitlines())},
        "outline": {"path": "outline.json", "section_count": len(sections)},
        "images": manifest_images,
        "tables": [],
        "evidence": {
            "default_ingest": False,
            "files": [blocks_path.relative_to(bundle_dir).as_posix()],
            "blocks": blocks_path.relative_to(bundle_dir).as_posix(),
        },
        "features": {
            "page_anchors": True,
            "outline": True,
            "external_tables": False,
            "figure_assets": True,
            "evidence_archive": True,
            "ocr_text": bool(text_count),
        },
        "counts": {
            "text": text_count,
            "image": len(input_images),
            "chart": 0,
            "table": 0,
            "equation": 0,
            "list": 0,
            "code": 0,
        },
        "quality": {"status": "warn", "issues": issues, "review_required": review_required},
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a governed Bundle v2 from standalone image files.")
    parser.add_argument("input", nargs="+", type=Path, help="Source image file(s)")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output image_document_bundle directory")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing bundle directory")
    parser.add_argument(
        "--material-type",
        choices=sorted(MATERIAL_TYPES),
        default="mixed",
        help="How to classify the standalone image source for QA policy",
    )
    parser.add_argument(
        "--ocr-text-file",
        type=Path,
        action="append",
        default=[],
        help="UTF-8 OCR text sidecar. Repeat once per image, unless there is only one image.",
    )
    parser.add_argument(
        "--ocr-command",
        help="Command template that prints OCR text to stdout. Use {input} for the image path.",
    )
    args = parser.parse_args()

    try:
        manifest = build_bundle(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Image bundle conversion failed: {exc}", file=sys.stderr)
        return 2

    print(f"Bundle: {args.output.expanduser().resolve()}")
    print(f"Schema: {manifest['schema_version']} ({manifest['profile']})")
    print(f"Quality: {manifest['quality']['status']}")
    print(f"Images: {len(manifest['images'])}")
    print(f"OCR text pages: {manifest['counts']['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
